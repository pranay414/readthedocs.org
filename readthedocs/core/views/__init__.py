"""
Core views.

Including the main homepage, documentation and header rendering,
and server errors.
"""

import os
import logging
from urllib.parse import urlparse

from django.conf import settings
from django.http import HttpResponseRedirect, Http404, JsonResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.views.generic import TemplateView
from django.views.static import serve as static_serve

from readthedocs.builds.models import Version
from readthedocs.core.utils.general import wipe_version_via_slugs
from readthedocs.core.resolver import resolve_path
from readthedocs.core.symlink import PrivateSymlink, PublicSymlink
from readthedocs.projects.constants import PRIVATE
from readthedocs.projects.models import HTMLFile, Project
from readthedocs.redirects.utils import (
    get_redirect_response,
    project_and_path_from_request,
    language_and_version_from_path
)

log = logging.getLogger(__name__)


class NoProjectException(Exception):
    pass


class HomepageView(TemplateView):

    template_name = 'homepage.html'

    def get_context_data(self, **kwargs):
        """Add latest builds and featured projects."""
        context = super().get_context_data(**kwargs)
        context['featured_list'] = Project.objects.filter(featured=True)
        context['projects_count'] = Project.objects.count()
        return context


class SupportView(TemplateView):
    template_name = 'support.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        support_email = settings.SUPPORT_EMAIL
        if not support_email:
            support_email = 'support@{domain}'.format(
                domain=settings.PRODUCTION_DOMAIN
            )

        context['support_email'] = support_email
        return context


def random_page(request, project_slug=None):  # pylint: disable=unused-argument
    html_file = HTMLFile.objects.order_by('?')
    if project_slug:
        html_file = html_file.filter(project__slug=project_slug)
    html_file = html_file.first()
    if html_file is None:
        raise Http404
    url = html_file.get_absolute_url()
    return HttpResponseRedirect(url)


def wipe_version(request, project_slug, version_slug):
    version = get_object_or_404(
        Version,
        project__slug=project_slug,
        slug=version_slug,
    )
    # We need to check by ``for_admin_user`` here to allow members of the
    # ``Admin`` team (which doesn't own the project) under the corporate site.
    if version.project not in Project.objects.for_admin_user(user=request.user):
        raise Http404('You must own this project to wipe it.')

    if request.method == 'POST':
        wipe_version_via_slugs(
            version_slug=version_slug,
            project_slug=project_slug,
        )
        return redirect('project_version_list', project_slug)
    return render(
        request,
        'wipe_version.html',
        {'version': version, 'project': version.project},
    )


def server_error_500(request, template_name='500.html'):
    """A simple 500 handler so we get media."""
    r = render(request, template_name)
    r.status_code = 500
    return r


def server_error_404(request, exception=None, template_name='404.html'):  # pylint: disable=unused-argument  # noqa
    """
    A simple 404 handler so we get media.

    .. note::

        Marking exception as optional to make /404/ testing page to work.
    """
    response = get_redirect_response(request, full_path=request.get_full_path())

    # Return a redirect response if there is one
    if response:
        if response.url == request.build_absolute_uri():
            # check that we do have a response and avoid infinite redirect
            log.warning(
                'Infinite Redirect: FROM URL is the same than TO URL. url=%s',
                response.url,
            )
        else:
            return response

    # Try to serve custom 404 pages if it's a subdomain/cname
    if getattr(request, 'subdomain', False) or getattr(request, 'cname', False):
        return server_error_404_subdomain(request, template_name)

    # Return the default 404 page generated by Read the Docs
    r = render(request, template_name)
    r.status_code = 404
    return r


def server_error_404_subdomain(request, template_name='404.html'):
    """
    Handler for 404 pages on subdomains.

    Check if the project associated has a custom ``404.html`` and serve this
    page. First search for a 404 page in the current version, then continues
    with the default version and finally, if none of them are found, the Read
    the Docs default page (Maze Found) is rendered by Django and served.
    """

    def resolve_404_path(project, version_slug=None, language=None, filename='404.html'):
        """
        Helper to resolve the path of ``404.html`` for project.

        The resolution is based on ``project`` object, version slug and
        language.

        :returns: tuple containing the (basepath, filename)
        :rtype: tuple
        """
        filename = resolve_path(
            project,
            version_slug=version_slug,
            language=language,
            filename=filename,
            subdomain=True,  # subdomain will make it a "full" path without a URL prefix
        )

        # This breaks path joining, by ignoring the root when given an "absolute" path
        if filename[0] == '/':
            filename = filename[1:]

        version = None
        if version_slug:
            version_qs = project.versions.filter(slug=version_slug)
            if version_qs.exists():
                version = version_qs.first()

        private = any([
            version and version.privacy_level == PRIVATE,
            not version and project.privacy_level == PRIVATE,
        ])
        if private:
            symlink = PrivateSymlink(project)
        else:
            symlink = PublicSymlink(project)
        basepath = symlink.project_root
        fullpath = os.path.join(basepath, filename)
        return (basepath, filename, fullpath)

    project, full_path = project_and_path_from_request(request, request.get_full_path())

    if project:
        language = None
        version_slug = None
        schema, netloc, path, params, query, fragments = urlparse(full_path)
        if not project.single_version:
            language, version_slug, path = language_and_version_from_path(path)

        # Firstly, attempt to serve the 404 of the current version (version_slug)
        # Secondly, try to serve the 404 page for the default version
        # (project.get_default_version())
        for slug in (version_slug, project.get_default_version()):
            for tryfile in ('404.html', '404/index.html'):
                basepath, filename, fullpath = resolve_404_path(project, slug, language, tryfile)
                if os.path.exists(fullpath):
                    log.debug(
                        'serving 404.html page current version: [project: %s] [version: %s]',
                        project.slug,
                        slug,
                    )
                    r = static_serve(request, filename, basepath)
                    r.status_code = 404
                    return r

    # Finally, return the default 404 page generated by Read the Docs
    r = render(request, template_name)
    r.status_code = 404
    return r


def do_not_track(request):
    dnt_header = request.META.get('HTTP_DNT')

    # https://w3c.github.io/dnt/drafts/tracking-dnt.html#status-representation
    return JsonResponse(  # pylint: disable=redundant-content-type-for-json-response
        {
            'policy': 'https://docs.readthedocs.io/en/latest/privacy-policy.html',
            'same-party': [
                'readthedocs.org',
                'readthedocs.com',
                'readthedocs.io',           # .org Documentation Sites
                'readthedocs-hosted.com',   # .com Documentation Sites
            ],
            'tracking': 'N' if dnt_header == '1' else 'T',
        }, content_type='application/tracking-status+json',
    )
