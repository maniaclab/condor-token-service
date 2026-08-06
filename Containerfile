FROM ghcr.io/prefix-dev/pixi:latest AS builder

WORKDIR /app
COPY . .

# Install only the service feature, not dev.
RUN pixi install --frozen --environment service

# Capture pixi's full activation (PATH, and anything else the environment
# needs) as a static entrypoint script, so the final image needs no pixi
# binary at runtime.
RUN echo '#!/bin/bash' > /app/entrypoint.sh && \
    pixi shell-hook --manifest-path /app/pixi.toml --environment service -s bash >> /app/entrypoint.sh && \
    echo 'exec "$@"' >> /app/entrypoint.sh && \
    chmod +x /app/entrypoint.sh

# Final stage. Base image choice: debian:bookworm-slim + the official
# HTCondor apt repository rather than htcondor/base. htcondor/base ships a
# full EL-based Condor daemon stack (hundreds of MB, its own supervisor
# entrypoint) when all this service needs is the condor_token_create CLI
# next to the Python app — and staying on bookworm keeps the pixi-built
# environment binary-compatible with the builder stage, matching
# af-mcp-platform's Containerfile.broker layout.
FROM debian:bookworm-slim
WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg && \
    curl -fsSL https://research.cs.wisc.edu/htcondor/repo/keys/HTCondor-24.x-Key \
      | gpg --dearmor -o /etc/apt/keyrings/htcondor.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/htcondor.gpg] https://research.cs.wisc.edu/htcondor/repo/debian/24.x bookworm main" \
      > /etc/apt/sources.list.d/htcondor.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends condor && \
    apt-get purge -y curl gnupg && apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

# Keep the same absolute path as the builder stage: the entrypoint script's
# activation exports (and any console-script shebangs, e.g. uvicorn) are
# baked in at this exact path, and relocating the env directory breaks them.
COPY --from=builder /app/.pixi/envs/service /app/.pixi/envs/service
COPY --from=builder /app/src /app/src
COPY --from=builder /app/entrypoint.sh /app/entrypoint.sh

ENV PYTHONPATH="/app/src" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# No USER directive: the runtime uid is governed by the Helm chart's
# podSecurityContext — reading the root-owned pool password typically
# requires runAsUser 0 (see charts/condor-token-service/values.yaml).

EXPOSE 8080
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["uvicorn", "condor_token_service.app:app", "--host", "0.0.0.0", "--port", "8080"]
