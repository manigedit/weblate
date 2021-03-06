# -*- coding: utf-8 -*-
#
# Copyright © 2012 - 2018 Michal Čihař <michal@cihar.com>
#
# This file is part of Weblate <https://weblate.org/>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

from __future__ import absolute_import, unicode_literals

from datetime import timedelta

from celery import shared_task

from django.db import transaction
from django.utils import timezone

from whoosh.index import EmptyIndexError

from weblate.auth.models import get_anonymous

from weblate.checks.models import Check

from weblate.lang.models import Language

from weblate.trans.models import (
    Suggestion, Comment, Unit, Project, Translation, Source, Component,
    Change,
)
from weblate.trans.search import Fulltext


@shared_task
def perform_update(cls, pk):
    if cls == 'Project':
        obj = Project.objects.get(pk=pk)
    else:
        obj = Component.objects.get(pk=pk)

    obj.do_update()


@shared_task
def perform_load(pk, *args):
    component = Component.objects.get(pk=pk)
    component.create_translations(*args)


@shared_task
def perform_commit(pk, *args):
    translation = Translation.objects.get(pk=pk)
    translation.commit_pending(*args)


@shared_task
def commit_pending(hours=None, pks=None, logger=None):
    if pks is None:
        translations = Translation.objects.all()
    else:
        translations = Translation.objects.filter(pk__in=pks)

    for translation in translations.prefetch():
        if not translation.repo_needs_commit():
            continue

        if hours is None:
            age = timezone.now() - timedelta(
                hours=translation.component.commit_pending_age
            )

        last_change = translation.last_change
        if last_change is None:
            continue
        if last_change > age:
            continue

        if logger:
            logger('Committing {0}'.format(translation))

        perform_commit.delay(translation.pk, 'commit_pending', None)


@shared_task
def cleanup_fulltext():
    """Remove stale units from fulltext"""
    fulltext = Fulltext()
    languages = list(Language.objects.have_translation().values_list(
        'code', flat=True
    ))
    # We operate only on target indexes as they will have all IDs anyway
    for lang in languages:
        index = fulltext.get_target_index(lang)
        try:
            fields = index.reader().all_stored_fields()
        except EmptyIndexError:
            continue
        for item in fields:
            if Unit.objects.filter(pk=item['pk']).exists():
                continue
            fulltext.clean_search_unit(item['pk'], lang)


@shared_task
def optimize_fulltext():
    fulltext = Fulltext()
    index = fulltext.get_source_index()
    index.optimize()
    languages = Language.objects.have_translation()
    for lang in languages:
        index = fulltext.get_target_index(lang.code)
        index.optimize()


def cleanup_sources(project):
    """Remove stale Source objects."""
    for pk in project.component_set.values_list('id', flat=True):
        with transaction.atomic():
            source_ids = Unit.objects.filter(
                translation__component_id=pk
            ).values('id_hash').distinct()

            Source.objects.filter(
                component_id=pk
            ).exclude(
                id_hash__in=source_ids
            ).delete()


def cleanup_source_data(project):
    with transaction.atomic():
        # List all current unit content_hashs
        units = Unit.objects.filter(
            translation__component__project=project
        ).values('content_hash').distinct()

        # Remove source comments and checks for deleted units
        for obj in Comment, Check:
            obj.objects.filter(
                language=None, project=project
            ).exclude(
                content_hash__in=units
            ).delete()


def cleanup_language_data(project):
    for lang in Language.objects.all():
        with transaction.atomic():
            # List current unit content_hashs
            units = Unit.objects.filter(
                translation__language=lang,
                translation__component__project=project
            ).values('content_hash').distinct()

            # Remove checks, suggestions and comments for deleted units
            for obj in Check, Suggestion, Comment:
                obj.objects.filter(
                    language=lang, project=project
                ).exclude(
                    content_hash__in=units
                ).delete()


@shared_task
def cleanup_project(pk):
    """Perform cleanup of project models."""
    project = Project.objects.get(pk=pk)

    cleanup_sources(project)
    cleanup_source_data(project)
    cleanup_language_data(project)


@shared_task
def cleanup_suggestions():
    # Process suggestions
    anonymous_user = get_anonymous()
    suggestions = Suggestion.objects.prefetch_related('project', 'language')
    for suggestion in suggestions.iterator():
        with transaction.atomic():
            # Remove suggestions with same text as real translation
            units = Unit.objects.filter(
                content_hash=suggestion.content_hash,
                translation__language=suggestion.language,
                translation__component__project=suggestion.project,
            )

            if not units.exclude(target=suggestion.target).exists():
                suggestion.delete_log(
                    anonymous_user,
                    Change.ACTION_SUGGESTION_CLEANUP
                )
                continue

            # Remove duplicate suggestions
            sugs = Suggestion.objects.filter(
                content_hash=suggestion.content_hash,
                language=suggestion.language,
                project=suggestion.project,
                target=suggestion.target
            ).exclude(
                id=suggestion.id
            )
            if sugs.exists():
                suggestion.delete_log(
                    anonymous_user,
                    Change.ACTION_SUGGESTION_CLEANUP
                )
