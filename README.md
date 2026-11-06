[![CI](https://github.com/DiamondLightSource/visr-tests/actions/workflows/ci.yml/badge.svg)](https://github.com/DiamondLightSource/visr-tests/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/DiamondLightSource/visr-tests/branch/main/graph/badge.svg)](https://codecov.io/gh/DiamondLightSource/visr-tests)

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)

# visr_tests

Scheduled component tests and end-to-end experiment runs for ViSR

`visr-tests` runs pytest checks against a ViSR beamline (b01-1) on a schedule,
as CronJobs from the beamline's services repo, and writes a JUnit report per
check. Component checks look at one service or device at a time; experiment
checks run a plan through blueapi and check the data it wrote to tiled.

What            | Where
:---:           | :---:
Source          | <https://github.com/DiamondLightSource/visr-tests>
Docker          | `docker run ghcr.io/diamondlightsource/visr-tests:latest`
Releases        | <https://github.com/DiamondLightSource/visr-tests/releases>

## Running checks

```
visr-tests run CHECK [ARGUMENT ...] [CHECK [ARGUMENT ...] ...]
```

Each check runs in its own pytest session, in the order given. Reports go to
`$REPORT_DIR/<timestamp>/$JOB_NAME/<check>.xml`. The exit status is non-zero
if any check fails.

Check                      | What it checks
:---                       | :---
`components/blueapi`       | blueapi `/healthz` answers 200 through its public URL
`components/tiled`         | tiled `/healthz`, an anonymous search, and that `/api/v1/binned/<unknown run>` answers 422 (the `dls_tiled.visr` router is mounted)
`components/flipper`       | each flipper (PV prefixes as arguments, e.g. `BL01C-EA-FLIP-01`) answers `caget <prefix>:Position`, and its `Position_RBV` is 0 or 1 and not in INVALID alarm
`components/align`         | each alignment stage (PV prefixes as arguments, e.g. `BL01C-EA-ALIGN-01`) answers `caget <prefix>:Pitch:Acceleration_RBV`
`components/panda`         | each PandA (PV prefixes as arguments, e.g. `BL01C-EA-PANDA-01 BL01C-MO-PPANDA-01`) answers `pvget <prefix>:Pcap:Active`
`components/iocs`          | each IOC in `IOC_NAMES` answers `caget <IOC NAME>:UPTIME` (devIocStats)
`experiments/spectroscopy` | runs `demo_spectroscopy` as a step scan and as a fly scan through blueapi, then finds each run in tiled by `start.blueapi_task_id` and checks its exit status is `success`, it has 4 primary events, and `/binned?width=2&height=2` gives a 2x2 `RedTotal`
`experiments/tomography`   | the tomography experiment
`ui/spectroscopy`          | in headless Chromium, as the service account: opens the ViSR UI's Spectroscopy page, presses Run with the form's default parameters, waits for "Plan succeeded", then checks through blueapi's task API that the task completed successfully. Needs the `-browser` image

The PV checks only read: they never write a PV or move anything. A PV must
answer within `PV_TIMEOUT` seconds; one that does not is tried once more before
the check fails. Moving each flipper between its two positions and back is a
possible future check.

The scans move the sample stage; the fly scan also needs the PandA and PMAC.
The plan, its parameters and the expected results are in
`src/visr_tests/experiments/spectroscopy.py`.

### The UI check

`ui/spectroscopy` drives the ViSR web UI (`b01-1-sci-ui`, the
[atlas](https://github.com/DiamondLightSource/atlas) `apps/visr` SPA) the way
a user does. Its selectors follow the deployed tag, `visr@0.5.4-auth-test-10`,
and will need updating when the deployment moves to a later UI.

- It first asks blueapi for the worker state. If the worker is not `IDLE`,
  another scan is running: the check fails and submits nothing. It never
  queues behind another scan.
- It opens `$UI_URL/acquisition/spectroscopy` with the instrument session set
  to `INSTRUMENT_SESSION`, checks the UI shows the service account logged in,
  and presses Run with the form's defaults (`demo_spectroscopy`, 25 points,
  5 mm grid, 0.1 s exposure). The scan is real and moves the sample stage.
- It waits up to `SCAN_TIMEOUT` seconds, from the start of the check, for the
  UI to report "Plan succeeded", then confirms with blueapi that the task
  completed with a success outcome. If the check fails once the UI may have
  started the scan (the timeout, a slow answer to the submission, an error
  the UI shows), the page is stopped from starting any task, and each task it
  did start is aborted if it is still the worker's active task. A task the
  page did not start is never aborted.
- When the scan finishes the UI submits the `visr-reconstruction` workflow
  for the instrument session, as it does for every scan.

The browser holds no login session: it sends the service account's
`client_credentials` token as `Authorization: Bearer` on requests to the
UI's own origin (`UI_URL`) only, never to any other origin, such as the PV
camera stream or the identity provider, not even through a redirect. A
redirect within the UI's origin is followed with the token. The page's event
streams from the UI are opened by a stand-in for the browser's `EventSource`
that never follows a redirect. For the token to work, the UI's oauth2-proxy (in
`b01-1-services`, `services/b01-1-sci-ui`) must accept bearer tokens from the
identity provider, with the service account's audience:

```
skip_jwt_bearer_tokens = true
extra_jwt_issuers = ["https://identity.diamond.ac.uk/realms/dls=account"]
```

Without them the UI shows logged out and the check fails saying so. If the
check fails, a screenshot of the page is saved beside its JUnit report, as
`<check>.png`. Neither the token nor request headers are logged or saved.

The bearer is only added to requests for the UI's own origin. This guards
against ordinary redirects, not against hostile script in the UI page or a
redirect `Location` crafted to parse differently in Python and Chromium;
the check only ever loads our own UI, so those are accepted.

### Environment

Variable             | Default | Used by
:---                 | :---    | :---
`BLUEAPI_URL`        | `https://b01-1-blueapi.diamond.ac.uk` | blueapi checks. The public URL, so requests go through oauth2-proxy as users' do
`TILED_URL`          | `http://b01-1-tiled:8000` | tiled checks. The in-cluster Service
`TOKEN_URL`          | `https://identity.diamond.ac.uk/realms/dls/protocol/openid-connect/token` | experiments
`CLIENT_ID`          | none | experiments and UI checks: the Keycloak service account's client id
`CLIENT_SECRET`      | none | experiments and UI checks: its client secret
`INSTRUMENT_SESSION` | none | experiments and UI checks: the instrument session the plans run in, e.g. `cm44191-4`. The service account must be allowed to run plans in it
`SCAN_TIMEOUT`       | `300` | experiments: seconds each scan may take, from submitting it to its run stopping in tiled. `ui/spectroscopy`: seconds from the start of the check to the UI reporting the outcome
`PV_TIMEOUT`         | `5` | PV checks: seconds to wait for each PV read, as `caget -w 5`
`UI_URL`             | `https://visr.diamond.ac.uk` | `ui/spectroscopy`: the ViSR UI, through its oauth2-proxy
`IOC_NAMES`          | none | `components/iocs`: the IOCs to check, separated by commas or spaces, e.g. `bl01c-di-dcam-01,bl01c-mo-ioc-01`. Each must load devIocStats (`devIocStats.iocAdminSoft` in its `ioc.yaml`)
`EPICS_CA_NAME_SERVERS` | none | PV checks over CA: in the cluster the gateway, `b01-1-epics-gateways:9064`
`EPICS_PVA_NAME_SERVERS` | none | PV checks over PVA: in the cluster the gateway, `b01-1-epics-gateways:9075`
`EPICS_CA_AUTO_ADDR_LIST`, `EPICS_PVA_AUTO_ADDR_LIST` | `YES` | set both to `NO` in the cluster, so PVs are searched for only through the gateway

Experiments authenticate to blueapi with an OAuth2 `client_credentials` token
(`client_secret_basic`), fetched once per check. A check that needs a variable
which is not set fails at setup and says which. The client secret and the token
are never logged or written to reports.

The PV checks use the standard EPICS client environment above (aioca for CA,
p4p for PVA), as blueapi does in `b01-1-blueapi/values.yaml`. In the cluster
the components job needs no host network: it reaches the IOCs through the
gateway Service.

For example, against the beamline from a workstation (tiled through its
LoadBalancer address):

```
CLIENT_ID=... CLIENT_SECRET=... INSTRUMENT_SESSION=cm44191-4 \
TILED_URL=http://<tiled LoadBalancer IP>:8000 \
    uv run visr-tests run components/blueapi components/tiled experiments/spectroscopy
```

and the PV checks through the gateway's LoadBalancer address:

```
EPICS_CA_NAME_SERVERS=<gateway IP>:9064 EPICS_CA_AUTO_ADDR_LIST=NO \
EPICS_PVA_NAME_SERVERS=<gateway IP>:9075 EPICS_PVA_AUTO_ADDR_LIST=NO \
IOC_NAMES=bl01c-di-dcam-01,bl01c-di-dcam-02,bl01c-di-dcam-04,bl01c-di-dcam-05,bl01c-di-locum-01,bl01c-mo-ioc-01 \
    uv run visr-tests run \
        components/flipper BL01C-EA-FLIP-01 BL01C-EA-FLIP-02 BL01C-EA-FLIP-03 \
        components/align BL01C-EA-ALIGN-01 \
        components/panda BL01C-EA-PANDA-01 BL01C-MO-PPANDA-01 \
        components/iocs
```

## The -browser image

The UI checks need Playwright and Chromium, which the default image leaves
out. CI builds a second image from the same source, the Dockerfile's
`browser` target, on Playwright's own image
(`mcr.microsoft.com/playwright/python`), tagged `<version>-browser` and
`latest-browser`:

```
docker run ghcr.io/diamondlightsource/visr-tests:<version>-browser run ui/spectroscopy
```

It runs every check the default image does. Playwright comes from the `ui`
extra, pinned to the version of the base image: bump them together. To run
the UI check outside the image, install the extra and Chromium:

```
uv sync --extra ui
uv run playwright install --with-deps chromium
CLIENT_ID=... CLIENT_SECRET=... INSTRUMENT_SESSION=cm44191-4 \
    uv run visr-tests run ui/spectroscopy
```

In the cluster it runs as its own CronJob, `b01-1-nightly-ui`, at 03:00, on
the `-browser` image with the same `visr-tests-secret` and
`INSTRUMENT_SESSION` as the experiments job.

The tests in `tests/test_ui.py` run the check in a real browser against a
build of the UI and fake backends (`tests/fake_ui.py`). They are skipped
unless the `ui` extra is installed and `VISR_UI_DIST` names a built
`apps/visr/dist` from atlas at the deployed tag, so CI skips them: run them
locally before a release:

```
VISR_UI_DIST=<atlas>/apps/visr/dist uv run --extra ui pytest tests/test_ui.py
```

## Provisioning the client secret

The experiment checks run as a Keycloak service account. Its client secret
reaches the CronJob as a SealedSecret in the services repo.

1.  Request the service account at the
    [Keycloak service desk](https://jira.diamond.ac.uk/servicedesk/customer/portal/5)
    with:

    - audience: `account`
    - instrument: `b01-1`
    - client id: `b01-1-blueapi-test-runner`
    - environment: Dev/Test and Production
    - your GPG public key, so the secret can be sent to you encrypted (see
      [request a registration with Keycloak](https://dev-guide.diamond.ac.uk/authn/how-tos/request-a-registration-with-keycloak/))

    You may receive separate credentials for identity, identity-dev and
    identity-test. Use the identity (production) ones: `TOKEN_URL` defaults
    to it.

2.  Decrypt the secret and seal it as the Secret `visr-tests-secret` in the
    `b01-1-beamline` namespace, with keys `client-id` and `client-secret`.
    From a clone of `b01-1-services`, with your GPG key available:

    ```bash
    module load k8s-b01-1   # kubectl and kubeseal for the b01-1 cluster

    tmp=$(mktemp -d)
    gpg --decrypt client-secret.pgp | tr -d '\r\n' > "$tmp/client-secret"
    kubectl create secret generic visr-tests-secret \
        --namespace b01-1-beamline \
        --from-literal=client-id=b01-1-blueapi-test-runner \
        --from-file=client-secret="$tmp/client-secret" \
        --dry-run=client -o yaml |
        kubeseal --format yaml \
        > services/b01-1-nightly-experiments/templates/secret.yaml
    rm -rf "$tmp"
    ```

    Replace `client-secret.pgp` with the file you received. kubeseal uses the
    cluster's sealed-secrets controller with its default name and namespace,
    as the other SealedSecrets in `b01-1-services` do. The sealed values only
    decrypt as `visr-tests-secret` in `b01-1-beamline`: do not rename it. The
    SealedSecret is safe to commit; the decrypted file is not, so make sure
    it is gone.

3.  Point the CronJob at it, in
    `services/b01-1-nightly-experiments/values.yaml`:

    ```yaml
    scheduled-jobs:
      env:
        - name: CLIENT_ID
          valueFrom:
            secretKeyRef: {name: visr-tests-secret, key: client-id}
        - name: CLIENT_SECRET
          valueFrom:
            secretKeyRef: {name: visr-tests-secret, key: client-secret}
        - name: INSTRUMENT_SESSION
          value: cm44191-4
    ```

    The components job needs no credentials. A Secret is per namespace, so
    another service in `b01-1-beamline` can reference `visr-tests-secret` the
    same way.
