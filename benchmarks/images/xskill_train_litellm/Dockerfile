ARG BASE_IMAGE=localhost:5000/p_user1/algo-xskill-officeqa:hard-canary-noforce-fix-20260909
FROM ${BASE_IMAGE}

ARG BASE_IMAGE
LABEL io.xarena.experiment="xskill-train-litellm-endpoints"
LABEL io.xarena.base-image="${BASE_IMAGE}"
LABEL io.xarena.note="EXPERIMENT ONLY: makes the upstream endpoint overridable so train traffic goes through LiteLLM. Algorithm code untouched; old tags not overwritten."

COPY patch_litellm.sh /tmp/patch_litellm.sh
RUN bash /tmp/patch_litellm.sh && rm -f /tmp/patch_litellm.sh
