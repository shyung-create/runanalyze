.PHONY: refresh refresh-push sample serve

refresh:
	python pipeline/refresh.py

refresh-push:
	python pipeline/refresh.py --push

# Regenerate demo data (no GarminDB or API key needed)
sample:
	python pipeline/sample_data.py

# Preview the dashboard locally at http://localhost:8000
serve:
	cd docs && python -m http.server 8000
