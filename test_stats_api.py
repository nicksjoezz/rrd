import json
from main import app
with app.test_client() as client:
    response = client.get('/api/stats')
    print(response.data.decode())
