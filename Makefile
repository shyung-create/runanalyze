.PHONY: refresh sample serve

refresh:
	python pipeline/refresh.py

# Regenerate demo data (no GarminDB or API key needed)
sample:
	python pipeline/sample_data.py

# Preview the dashboard locally at http://localhost:8000
serve:
	cd docs && python -m http.server 8000
