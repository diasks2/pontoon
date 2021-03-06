
import base64
import codecs
import configparser
import commonware
import datetime
import fnmatch
import json
import os
import shutil
import polib
import silme.core
import silme.format.properties
import urllib2

from django.conf import settings
from django.core.urlresolvers import reverse
from django.contrib import messages
from django.db import transaction
from django.forms.models import inlineformset_factory
from django.http import HttpResponse, HttpResponseRedirect, Http404
from django.shortcuts import render
from django.template.defaultfilters import slugify
from django.utils.datastructures import MultiValueDictKeyError
from django.utils.translation import ugettext_lazy as _

from pontoon.administration.utils.vcs import (
    PullFromRepositoryException,
    update_from_vcs,
)

from pontoon.base.models import (
    Entity,
    Locale,
    Project,
    ProjectForm,
    Subpage,
    Translation,
    UserProfile,
)

from pontoon.base.views import _request


log = commonware.log.getLogger('pontoon')


def admin(request, template='admin.html'):
    """Admin interface."""
    log.debug("Admin interface.")

    if not request.user.has_perm('base.can_manage'):
        return render(request, '403.html', status=403)

    data = {
        'projects': Project.objects.all(),
    }

    return render(request, template, data)


def get_slug(request):
    """Convert project name to slug."""
    log.debug("Convert project name to slug.")

    if not request.user.has_perm('base.can_manage'):
        log.error("Insufficient privileges.")
        return HttpResponse("error")

    if not request.is_ajax():
        log.error("Non-AJAX request")
        return HttpResponse("error")

    try:
        name = request.GET['name']
    except MultiValueDictKeyError as e:
        log.error(str(e))
        return HttpResponse("error")

    log.debug("Name: " + name)

    slug = slugify(name)
    log.debug("Slug: " + slug)
    return HttpResponse(slug)


def manage_project(request, slug=None, template='project.html'):
    """Admin interface: manage project."""
    log.debug("Admin interface: manage project.")

    if not request.user.has_perm('base.can_manage'):
        return render(request, '403.html', status=403)

    SubpageInlineFormSet = inlineformset_factory(Project, Subpage, extra=1)
    form = ProjectForm()
    formset = SubpageInlineFormSet()
    locales_selected = []
    subtitle = 'Add project'
    pk = None
    project = None
    message = 'Before localizing projects, \
               you need to import strings from the repository.'

    if request.method == 'POST':
        locales_selected = Locale.objects.filter(
            pk__in=request.POST.getlist('locales'))
        # Update existing project
        try:
            pk = request.POST['pk']
            project = Project.objects.get(pk=pk)
            form = ProjectForm(request.POST, instance=project)
            # Needed if form invalid
            formset = SubpageInlineFormSet(request.POST, instance=project)
            subtitle = 'Edit project'

        # Add a new project
        except MultiValueDictKeyError:
            form = ProjectForm(request.POST)
            # Needed if form invalid
            formset = SubpageInlineFormSet(request.POST)

        if form.is_valid():
            project = form.save(commit=False)
            formset = SubpageInlineFormSet(request.POST, instance=project)
            if formset.is_valid():
                project.save()
                # http://bit.ly/1glKN50
                form.save_m2m()
                formset.save()
                # Properly displays formset, but removes errors (if valid only)
                formset = SubpageInlineFormSet(instance=project)
                subtitle += '. Saved.'
                pk = project.pk
                if len(Entity.objects.filter(project=project)) is 0:
                    messages.warning(request, message)
            else:
                subtitle += '. Error.'
        else:
            subtitle += '. Error.'

    # If URL specified and found, show edit, otherwise show add form
    elif slug is not None:
        try:
            project = Project.objects.get(slug=slug)
            pk = project.pk
            form = ProjectForm(instance=project)
            formset = SubpageInlineFormSet(instance=project)
            locales_selected = project.locales.all()
            subtitle = 'Edit project'
            if len(Entity.objects.filter(project=project)) is 0:
                messages.warning(request, message)
        except Project.DoesNotExist:
            form = ProjectForm(initial={'slug': slug})

    data = {
        'form': form,
        'formset': formset,
        'locales_selected': locales_selected,
        'locales_available': Locale.objects.exclude(pk__in=locales_selected),
        'REPOSITORY_TYPE_CHOICES': Project.REPOSITORY_TYPE_CHOICES,
        'subtitle': subtitle,
        'pk': pk,
    }

    if len(Entity.objects.filter(project=project)) is not 0:
        data['ready'] = True

    return render(request, template, data)


@transaction.commit_manually
def delete_project(request, pk, template=None):
    """Admin interface: delete project."""
    try:
        log.debug("Admin interface: delete project.")

        if not request.user.has_perm('base.can_manage'):
            return render(request, '403.html', status=403)

        project = Project.objects.get(pk=pk)
        project.delete()

        path = os.path.join(
            settings.MEDIA_ROOT, project.repository_type, project.slug)
        if os.path.exists(path):
            shutil.rmtree(path)

        transaction.commit()
        return HttpResponseRedirect(reverse('pontoon.admin'))
    except Exception as e:
        log.error(
            "Admin interface: delete project error.\n%s"
            % unicode(e), exc_info=True)
        transaction.rollback()
        messages.error(
            request,
            "There was an error during deleting this project.")
        return HttpResponseRedirect(reverse(
            'pontoon.admin.project',
            args=[project.slug]))


def _save_entity(project, string, string_plural="",
                 comment="", key="", source=""):
    """Admin interface: save new or update existing entity in DB."""

    # Update existing entity
    try:
        if key is "":
            e = Entity.objects.get(
                project=project, string=string, string_plural=string_plural)
        else:
            e = Entity.objects.get(project=project, key=key, source=source)
            e.string = string
            e.string_plural = string_plural

    # Add new entity
    except Entity.DoesNotExist:
        e = Entity(project=project, string=string, string_plural=string_plural,
                   key=key, source=source)

    if len(comment) > 0:
        e.comment = comment
    e.save()


def _save_translation(entity, locale, string, plural_form=None, fuzzy=False):
    """Admin interface: save new or update existing translation in DB."""

    approved = not fuzzy

    # Update existing translation if different from repository
    try:
        t = Translation.objects.get(entity=entity, locale=locale,
                                    plural_form=plural_form, approved=True)
        if t.string != string or t.fuzzy != fuzzy:
            t.string = string
            t.user = None
            t.date = datetime.datetime.now()
            t.approved = approved
            t.fuzzy = fuzzy
            t.save()

    # Save new translation if it doesn's exist yet
    except Translation.DoesNotExist:
        t = Translation(
            entity=entity, locale=locale, string=string,
            plural_form=plural_form, date=datetime.datetime.now(),
            approved=approved, fuzzy=fuzzy)
        t.save()


def _get_locale_paths(source_paths, source_directory, locale_code):
    """Get paths to locale files."""

    locale_paths = []
    for sp in source_paths:

        # Also include paths to source files
        if source_directory == locale_code:
            path = sp
            locale_paths.append(path)

        else:
            path = sp.replace('/' + source_directory + '/',
                              '/' + locale_code + '/', 1).rstrip("t")

            # Only include if path exists
            if os.path.exists(path):
                locale_paths.append(path)

            # Also check for locale variants with underscore, e.g. de_AT
            elif locale_code.find('-') != -1:
                path = path.replace('/' + locale_code + '/',
                                  '/' + locale_code.replace('-', '_') + '/', 1)

                if os.path.exists(path):
                    locale_paths.append(path)

    return locale_paths


def _get_format_and_source_paths(path):
    """Get file format based on extensions and paths to source files."""
    log.debug("Get file format based on extensions and paths to source files.")

    format = None
    source_paths = []
    for root, dirnames, filenames in os.walk(path):
        # Ignore hidden files and folders
        filenames = [f for f in filenames if not f[0] == '.']
        dirnames[:] = [d for d in dirnames if not d[0] == '.']

        for extension in ('pot', 'po', 'properties', 'ini', 'lang'):
            for filename in fnmatch.filter(filenames, '*.' + extension):
                if format is None:
                    format = 'po' if extension == 'pot' else extension
                source_paths.append(os.path.join(root, filename))

    return format, source_paths


def _get_source_directory(path):
    """Get name and path of the directory with source strings."""
    log.debug("Get name and path of the directory with source strings.")

    for root, dirnames, filenames in os.walk(path):
        # Ignore hidden files and folders
        filenames = [f for f in filenames if not f[0] == '.']
        dirnames[:] = [d for d in dirnames if not d[0] == '.']

        for directory in ('templates', 'en-US', 'en-GB', 'en'):
            for dirname in fnmatch.filter(dirnames, directory):
                return dirname, root

    # INI Format
    return '', path


def _is_one_locale_repository(repository_url, repository_path_master):
    """Check if repository contains one or multiple locales."""

    source_directory = repository_url_master = False
    repository_path = repository_path_master
    last = os.path.basename(os.path.normpath(repository_url))

    if last in ('templates', 'en-US', 'en-GB', 'en'):
        source_directory = last
        repository_url_master = repository_url.rsplit(last, 1)[0]
        repository_path = os.path.join(
            repository_path_master, source_directory)

    return source_directory, repository_url_master, repository_path


def _parse_lang(path):
    """Parse a dotlang file and return a dict of translations."""
    trans = {}

    if not os.path.exists(path):
        return trans

    with codecs.open(path, 'r', 'utf-8', errors='replace') as lines:
        source = None
        comment = ''

        for line in lines:
            line = line.strip()
            if not line:
                continue

            if line[0] == '#' and line[1] != '#':
                comment = line.lstrip('#').strip()
                continue

            if line[0] == ';':
                source = line[1:]

            elif source:
                for tag in ('{ok}', '{l10n-extra}'):
                    if line.lower().endswith(tag):
                        line = line[:-len(tag)]
                line = line.strip()
                trans[source] = [comment, line]
                comment = ''

    return trans


def _extract_po(project, locale, paths, source_locale, translations=True):
    """Extract .po (gettext) files from paths and save or update in DB."""

    for path in paths:
        try:
            log.debug(path)
            po = polib.pofile(path)
            escape = polib.escape

            if locale.code == source_locale:
                for entry in po:
                    if not entry.obsolete:
                        _save_entity(project=project,
                                     string=escape(entry.msgid),
                                     string_plural=escape(entry.msgid_plural),
                                     comment=entry.comment,
                                     source=entry.occurrences)
            elif translations:
                for entry in (po.translated_entries() + po.fuzzy_entries()):
                    if not entry.obsolete:

                        # Entities without plurals
                        if len(escape(entry.msgstr)) > 0:
                            try:
                                e = Entity.objects.get(project=project,
                                    string=escape(entry.msgid))
                                _save_translation(
                                    entity=e,
                                    locale=locale,
                                    string=escape(entry.msgstr),
                                    fuzzy='fuzzy' in entry.flags)

                            except Entity.DoesNotExist:
                                continue

                        # Pluralized entities
                        elif len(entry.msgstr_plural) > 0:
                            try:
                                e = Entity.objects.get(project=project,
                                    string=escape(entry.msgid))
                                for k in entry.msgstr_plural:
                                    _save_translation(
                                        entity=e,
                                        locale=locale,
                                        string=escape(entry.msgstr_plural[k]),
                                        plural_form=k,
                                        fuzzy='fuzzy' in entry.flags)

                            except Entity.DoesNotExist:
                                continue

            log.debug("[" + locale.code + "]: saved to DB.")
        except Exception as e:
            log.critical('PoExtractError for %s: %s' % (path, e))


def _extract_properties(project, locale, paths,
                        source_locale, translations=True):
    """Extract .properties files from paths and save or update in DB."""

    for path in paths:
        try:
            f = open(path)
            l10nobject = silme.format.properties \
                .PropertiesFormatParser.get_structure(f.read())

            locale_code = locale.code
            if 'templates' in path:
                locale_code = 'templates'
            short_path = '/' + path.split('/' + locale_code + '/')[-1]

            for line in l10nobject:
                if isinstance(line, silme.core.entity.Entity):
                    if locale.code == source_locale:
                        _save_entity(project=project, string=line.value,
                                     key=line.id, source=short_path)
                    elif translations:
                        try:
                            e = Entity.objects.get(
                                project=project,
                                key=line.id,
                                source=short_path)
                            _save_translation(
                                entity=e,
                                locale=locale,
                                string=line.value)
                        except Entity.DoesNotExist:
                            continue
            log.debug("[" + locale.code + "]: " + path + " saved to DB.")
            f.close()
        except IOError:
            log.debug("[" + locale.code + "]: " +
                      path + " doesn't exist. Skipping.")


def _extract_lang(project, locale, paths, source_locale, translations=True):
    """Extract .lang files from paths and save or update in DB."""

    for path in paths:
        lang = _parse_lang(path)

        if locale.code == source_locale:
            for key, value in lang.items():
                _save_entity(project=project, string=key, comment=value[0])
        elif translations:
            for key, value in lang.items():
                if key != value[1]:
                    try:
                        e = Entity.objects.get(project=project, string=key)
                        _save_translation(
                            entity=e, locale=locale, string=value[1])
                    except Entity.DoesNotExist:
                        continue

        log.debug("[" + locale.code + "]: saved to DB.")


def _extract_ini(project, path):
    """Extract .ini file from path and save or update in DB."""

    config = configparser.ConfigParser()
    with codecs.open(path, 'r', 'utf-8') as f:
        try:
            config.read_file(f)
        except Exception, e:
            log.debug("INI configparser: " + str(e))
            raise Exception("error")

    sections = config.sections()

    source_locale = None
    for s in ('templates', 'en-US', 'en-GB', 'en'):
        if s in sections:
            source_locale = s
            break
    if source_locale is None:
        log.error("Unable to detect source locale")
        raise Exception("error")

    # Move source locale on top, so we save entities first, then translations
    sections.insert(0, sections.pop(sections.index(source_locale)))

    for section in sections:
        for item in config.items(section):
            if section == source_locale:
                _save_entity(project=project, string=item[1],
                             key=item[0], source=path)
            else:
                try:
                    l = Locale.objects.get(code=section)
                except Locale.DoesNotExist:
                    log.debug("Locale not supported: " + section)
                    break
                try:
                    e = Entity.objects.get(
                        project=project, key=item[0], source=path)
                    _save_translation(
                        entity=e, locale=l, string=item[1])
                except Entity.DoesNotExist:
                    log.debug("[" + section + "]: line ID " +
                              item[0] + " is obsolete.")
                    continue
        log.debug("[" + section + "]: saved to DB.")


def _update_from_repository(
        project, repository_type, repository_url, repository_path_master):

    if repository_type == 'file':
        file_name = repository_url.rstrip('/').rsplit('/', 1)[1]

        temp, file_extension = os.path.splitext(file_name)
        format = file_extension[1:].lower()
        if format == 'pot':
            format = 'po'
        project.format = format
        project.repository_path = repository_path_master
        project.save()

        # Store file to server
        u = urllib2.urlopen(repository_url)
        file_path = os.path.join(repository_path_master, file_name)
        if not os.path.exists(repository_path_master):
            os.makedirs(repository_path_master)
        try:
            with open(file_path, 'w') as f:
                f.write(u.read().decode("utf-8-sig").encode("utf-8"))
        except IOError, e:
            log.debug("IOError: " + str(e))
            raise PullFromRepositoryException(unicode(e))

        if format in ('po', 'properties', 'lang'):
            source_locale = 'en-US'
            locales = [Locale.objects.get(code=source_locale)]
            locales.extend(project.locales.all())

            for l in locales:
                if format == 'po':
                    _extract_po(
                        project, l, [file_path], source_locale, False)
                elif format == 'properties':
                    _extract_properties(
                        project, l, [file_path], source_locale, False)
                elif format == 'lang':
                    _extract_lang(
                        project, l, [file_path], source_locale, False)

        elif format == 'ini':
            try:
                _extract_ini(project, file_path)
            except Exception, e:
                os.remove(file_path)
                raise PullFromRepositoryException(unicode(e))

        else:
            log.error("Format not supported")
            raise PullFromRepositoryException("Not supported")

    elif repository_type in ('git', 'hg', 'svn'):
        """ Mercurial """
        # Update repository URL and path if one-locale repository
        source_directory, repository_url_master, repository_path = \
            _is_one_locale_repository(repository_url, repository_path_master)

        update_from_vcs(repository_type, repository_url, repository_path)

        # Get file format and paths to source files
        if source_directory is False:
            source_directory, source_directory_path = \
                _get_source_directory(repository_path)
            format, source_paths = \
                _get_format_and_source_paths(
                    os.path.join(source_directory_path, source_directory))
        else:
            format, source_paths = \
                _get_format_and_source_paths(repository_path)

            # Get remaining repositories if one-locale repository specified
            for l in project.locales.all():
                update_from_vcs(
                    repository_type,
                    os.path.join(repository_url_master, l.code),
                    os.path.join(repository_path_master, l.code))

        project.format = format
        project.repository_path = repository_path
        project.save()

        if format in ('po', 'properties', 'lang'):
            # Extend project locales array with source locale
            if source_directory == 'templates':
                source_locale = 'en-US'
            else:
                source_locale = source_directory
            locales = [Locale.objects.get(code=source_locale)]
            locales.extend(project.locales.all())

            for index, l in enumerate(locales):
                # source_directory could also be called templates
                locale_code = l.code
                if index == 0:
                    locale_code = source_directory
                locale_paths = \
                    _get_locale_paths(
                        source_paths, source_directory, locale_code)

                if format == 'po':
                    _extract_po(
                        project, l, locale_paths, source_locale)
                elif format == 'properties':
                    _extract_properties(
                        project, l, locale_paths, source_locale)
                elif format == 'lang':
                    _extract_lang(
                        project, l, locale_paths, source_locale)

        elif format == 'ini':
            try:
                _extract_ini(project, source_paths[0])
            except Exception, e:
                raise PullFromRepositoryException(unicode(e))

        else:
            log.error("Format not supported")
            raise PullFromRepositoryException("Not supported")

    else:
        log.error("Repository type not supported")
        raise PullFromRepositoryException("Not supported")


def update_from_repository(request, template=None):
    """Update all project locales from repository."""
    log.debug("Update all project locales from repository.")

    if not request.user.has_perm('base.can_manage'):
        return render(request, '403.html', status=403)

    if request.method != 'POST':
        log.error("Non-POST request")
        raise Http404

    try:
        pk = request.POST['pk']
        repository_type = request.POST['repository_type']
        repository_url = request.POST['repository_url']
    except MultiValueDictKeyError as e:
        log.error(str(e))
        return HttpResponse("error")

    try:
        p = Project.objects.get(pk=pk)
    except Project.DoesNotExist as e:
        log.error(str(e))
        return HttpResponse("error")

    repository_path_master = os.path.join(
        settings.MEDIA_ROOT, repository_type, p.slug)
    try:
        _update_from_repository(
            p, repository_type, repository_url, repository_path_master)
    except PullFromRepositoryException as e:
        log.error("PullFromRepositoryException: " + str(e))
        return HttpResponse('error')
    except Exception as e:
        log.error("Exception: " + str(e))
        return HttpResponse('error')

    return HttpResponse("200")


def update_from_transifex(request, template=None):
    """Update all project locales from Transifex repository."""
    log.debug("Update all project locales from Transifex repository.")

    if not request.user.has_perm('base.can_manage'):
        return render(request, '403.html', status=403)

    if request.method != 'POST':
        log.error("Non-POST request")
        raise Http404

    try:
        pk = request.POST['pk']
        transifex_project = request.POST['transifex_project']
        transifex_resource = request.POST['transifex_resource']
    except MultiValueDictKeyError as e:
        log.error(str(e))
        return HttpResponse("error")

    try:
        p = Project.objects.get(pk=pk)
    except Project.DoesNotExist as e:
        log.error(str(e))
        return HttpResponse("error")

    """Check if user authenticated to Transifex."""
    profile, created = UserProfile.objects.get_or_create(user=request.user)
    username = request.POST.get(
        'transifex_username', profile.transifex_username)
    password = request.POST.get(
        'transifex_password', base64.decodestring(profile.transifex_password))

    if len(username) == 0 or len(password) == 0:
        return HttpResponse("authenticate")

    for l in p.locales.all():
        """Make GET request to Transifex API."""
        response = _request('get', transifex_project, transifex_resource,
                            l.code, username, password)

        """Save or update Transifex data to DB."""
        if hasattr(response, 'status_code') and response.status_code == 200:
            entities = json.loads(response.content)
            for entity in entities:
                _save_entity(project=p, string=entity["key"],
                             comment=entity["comment"])
                if len(entity["translation"]) > 0:
                    e = Entity.objects.get(project=p, string=entity["key"])
                    _save_translation(
                        entity=e, locale=l, string=entity["translation"])
            log.debug("Transifex data for " + l.name + " saved to DB.")
        else:
            return HttpResponse(response)

    """Save Transifex username and password."""
    if 'remember' in request.POST and request.POST['remember'] == "on":
        profile.transifex_username = request.POST['transifex_username']
        profile.transifex_password = base64.encodestring(
            request.POST['transifex_password'])
        profile.save()

    return HttpResponse(response.status_code)
