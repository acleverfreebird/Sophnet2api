FROM python:3.12

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Fetch Camoufox version information
RUN camoufox fetch

COPY . /app

CMD ["python", "main.py"]