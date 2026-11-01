# Runner for the contract tests (scripts/run_contract_tests.sh). The dependencies are installed when this
# image is built, so the test run itself needs no network. The build context is backup_panel/.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

WORKDIR /app
