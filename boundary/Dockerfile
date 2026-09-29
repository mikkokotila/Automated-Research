FROM python:3.12-slim
RUN pip install --no-cache-dir httpx==0.28.1 pypdf==6.19.0
COPY boundary /usr/local/lib/python3.12/site-packages/boundary
RUN mkdir /state && chown 10001:10001 /state
USER 10001:10001
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-I", "-m", "boundary.server"]
CMD ["serve"]
