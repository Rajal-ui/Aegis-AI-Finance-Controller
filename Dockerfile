FROM python:3.13-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY generate_finance_data.py reconcile_finance.py ./

RUN python -c "import src.models, src.reconcile"

ENTRYPOINT ["python", "reconcile_finance.py"]
CMD ["--input-dir", "synthetic_data", "--output", "reconciliation_report.json"]
