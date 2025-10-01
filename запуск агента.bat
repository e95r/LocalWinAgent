.\.venv\Scripts\activate.bat
pip install -r requirements.txt
python -m uvicorn main:app --host 127.0.0.1 --port 8765 --reload
