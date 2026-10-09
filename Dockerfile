# The devcontainer should use the developer target and run as root with podman
# or docker with user namespaces.
FROM ghcr.io/diamondlightsource/ubuntu-devcontainer:resolute AS developer

# Add any system dependencies for the developer/build environment here
RUN apt-get update -y && apt-get install -y --no-install-recommends \
    graphviz \
    && apt-get dist-clean

# The build stage installs the context into the venv
FROM developer AS build

# Change the working directory to the `app` directory
# and copy in the project
WORKDIR /app
COPY . /app
RUN chmod o+wrX .

# Tell uv sync to install python in a known location so we can copy it out later
ENV UV_PYTHON_INSTALL_DIR=/python

# Sync the project without its dev dependencies
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-editable --no-dev --managed-python

# The browser build adds the ui extra (Playwright) to the same venv
FROM build AS build-browser
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-editable --no-dev --extra ui --managed-python

# The -browser image: the runtime on Playwright's own image, which has Chromium
# and its system libraries at /ms-playwright. Its version must be the
# playwright pinned by the ui extra in pyproject.toml.
FROM mcr.microsoft.com/playwright/python:v1.63.0-resolute AS browser

COPY --from=build-browser /python /python
COPY --from=build-browser /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH

ENTRYPOINT ["visr-tests"]
CMD ["--version"]

# The runtime stage copies the built venv into a runtime container.
# It stays the last stage, so a build with no --target makes this image.
FROM ubuntu:resolute AS runtime

# Add apt-get system dependecies for runtime here if needed
# RUN apt-get update -y && apt-get install -y --no-install-recommends \
#     some-library \
#     && apt-get dist-clean

# Copy the python installation from the build stage
COPY --from=build /python /python

# Copy the environment, but not the source code
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH

# change this entrypoint if it is not the same as the repo
ENTRYPOINT ["visr-tests"]
CMD ["--version"]
