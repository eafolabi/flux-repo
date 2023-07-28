from typing import Iterator
import json
import b2
import prometheus_client.core
import kubernetes

from datetime import datetime
import dateutil.parser

from b2.utils import load_kubernetes_config, setup_logging


# Setup logging
log = setup_logging()

RECONCILE_ANNOTATION = 'kustomize.toolkit.fluxcd.io/reconcile'
SUSPEND_UNTIL_ANNOTATION = 'flux.b2.genesiscloud.dev/suspend-until'
SUSPENDED_BY_ANNOTATION = 'flux.b2.genesiscloud.dev/suspended-by'


load_kubernetes_config()
apiClient = kubernetes.client.ApiClient()


def getAllAPIs() -> Iterator[tuple[str, str]]:
    yield ('', 'v1')
    for api in kubernetes.client.ApisApi().get_api_versions().groups:
        yield (api.name, api.preferred_version.version)


def getAllResourceKindsInAPI(group: str, version: str) -> Iterator[tuple[str, str, str, bool]]:
    endpoint = f'/apis/{group}/{version}' if group else f'/api/{version}'
    response, status, _ = apiClient.call_api(
        endpoint, 'GET', auth_settings=['BearerToken'], response_type='json', _preload_content=False
    )
    assert status == 200
    for resource in json.loads(response.data)['resources']:
        yield (resource['name'], resource['kind'], resource['verbs'], resource['namespaced'])


def getAllResourcesInAPI(group: str, version: str, plural: str) -> Iterator[dict]:
    endpoint = f'/apis/{group}/{version}/{plural}' if group else f'/api/{version}/{plural}'
    response, status, _ = apiClient.call_api(
        endpoint, 'GET', auth_settings=['BearerToken'], response_type='json', _preload_content=False
    )
    assert status == 200
    yield from json.loads(response.data)['items']


def getAllResources() -> Iterator[tuple[str, str, str, str, bool, dict]]:
    for group, version in getAllAPIs():
        for plural, kind, verbs, namespaced in getAllResourceKindsInAPI(group, version):
            if not verbs or 'list' not in verbs:
                continue
            for resource in getAllResourcesInAPI(group, version, plural):
                yield group, version, plural, kind, namespaced, resource


def removeResourceAnnotation(
    group: str, version: str, plural: str, namespace: str, name: str, annotations: list[str]
):
    # Build the API endpoint
    endpoint = f'/apis/{group}/{version}' if group else f'/api/{version}'
    if namespace:
        endpoint += f'/namespaces/{namespace}'
    endpoint += f'/{plural}/{name}'

    # Build the JSON Patch
    body = []
    for annotation in annotations:
        # Escape annotation to JSON Patch format
        annotation = annotation.replace('~', '~0')
        annotation = annotation.replace('/', '~1')
        body.append({'op': 'remove', 'path': f'/metadata/annotations/{annotation}'})

    # Apply the JSON Patch
    headers = {'Content-Type': 'application/json-patch+json'}
    _, status, _ = apiClient.call_api(
        endpoint,
        'PATCH',
        auth_settings=['BearerToken'],
        response_type='json',
        _preload_content=False,
        header_params=headers,
        body=body,
    )
    assert status == 200


# Iterate over all resources, timeout suspension if applicable, alert on "unclean" suspension
def checkAllResources() -> Iterator[tuple[list, bool, bool]]:
    for group, version, plural, kind, namespaced, resource in getAllResources():
        meta = resource['metadata']
        annotations = meta.get('annotations', {})
        namespace = meta['namespace'] if namespaced else ''
        name = meta['name']
        resourceName = f"{group}/{version} " + (f"{namespace}/{name}" if namespace else name)

        # Ignore non-suspended resources
        if annotations.get(RECONCILE_ANNOTATION) != 'disabled':
            continue

        log.debug(f"Resource {resourceName} is suspended")

        # Apply suspension timeout, if specified
        hasTimeout = SUSPEND_UNTIL_ANNOTATION in annotations
        hasOwner = SUSPENDED_BY_ANNOTATION in annotations
        if hasTimeout:
            # Try to parse the timeout and check if it's past, if we fail to parse pretend that it
            # didn't exist (which will cause an alert)
            timeout = annotations[SUSPEND_UNTIL_ANNOTATION]
            try:
                timedOut = dateutil.parser.isoparse(timeout) < datetime.now()
            except dateutil.parser.ParserError:
                log.error(f"Failed to parse timestamp {timeout} on {resourceName}", exc_info=True)
                timedOut = False
                hasTimeout = False

            if timedOut:
                # Suspend has timed out, remove all annotations
                removeAnnotations = [RECONCILE_ANNOTATION, SUSPEND_UNTIL_ANNOTATION]
                if hasOwner:
                    removeAnnotations.append(SUSPENDED_BY_ANNOTATION)
                log.info(f"Removing suspend annotations from {resourceName}")
                removeResourceAnnotation(group, version, plural, namespace, name, removeAnnotations)
                continue

        # Form Prometheus labels
        labels = [f'{group}/{version}', kind, meta['namespace'] if namespaced else '', meta['name']]
        yield labels, hasTimeout, hasOwner


class FluxSuspendedCollector:
    LABELS = ['apiVersion', 'kind', 'namespace', 'name']

    def getGauges(self):
        return (
            prometheus_client.core.GaugeMetricFamily(
                'flux_suspended_resources',
                "Resources that are suspended from Flux reconciliation",
                labels=self.LABELS,
            ),
            prometheus_client.core.GaugeMetricFamily(
                'flux_suspended_resources_unowned',
                "Suspended resources that don't have an owner annotation",
                labels=self.LABELS,
            ),
            prometheus_client.core.GaugeMetricFamily(
                'flux_suspended_resources_indefinite',
                "Suspended resources that don't have a timeout",
                labels=self.LABELS,
            ),
        )

    def describe(self):
        yield from self.getGauges()

    def collect(self):
        gAll, gUnowned, gIndefinite = self.getGauges()
        for labels, hasTimeout, hasOwner in checkAllResources():
            gAll.add_metric(labels, 1)
            gUnowned.add_metric(labels, 0 if hasOwner else 1)
            gIndefinite.add_metric(labels, 0 if hasTimeout else 1)
        yield gAll
        yield gUnowned
        yield gIndefinite


prometheus_client.core.REGISTRY.register(FluxSuspendedCollector())

app = prometheus_client.make_wsgi_app()
