# One image for the router and controller Lambdas (TRD-AWS-Addendum §11.2).
# Base image and layout follow "Deploy Python Lambda functions with container images":
#   https://docs.aws.amazon.com/lambda/latest/dg/python-image.html
# Tag 3.14 (Python 3.14, Amazon Linux 2023) from https://gallery.ecr.aws/lambda/python,
# confirmed to publish a linux/arm64 manifest (`docker manifest inspect`).
# Build from the repo root:  docker build --platform linux/arm64 -t semauto-core -f cloud/Dockerfile .
FROM public.ecr.aws/lambda/python:3.14

# Dependencies first so code edits don't invalidate the slow install layer.
COPY cloud/requirements-lambda.txt ${LAMBDA_TASK_ROOT}/cloud/requirements-lambda.txt
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/cloud/requirements-lambda.txt

COPY src/ ${LAMBDA_TASK_ROOT}/src/
COPY configs/ ${LAMBDA_TASK_ROOT}/configs/
COPY cloud/ ${LAMBDA_TASK_ROOT}/cloud/

# Train the classifier at build time with the simulation's seeds (writes model/classifier.joblib).
RUN cd ${LAMBDA_TASK_ROOT} && python -m cloud.build_model

# The router is the default; the controller overrides this via ImageConfig.Command (§11.5).
CMD [ "cloud.handlers.router.handler" ]
