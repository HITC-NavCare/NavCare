import sys

from dotenv import load_dotenv

import orchestrator

# Example location: downtown Montréal.
EXAMPLE_LAT = 45.5017
EXAMPLE_LON = -73.5673

if __name__ == "__main__":
    # Read local API keys from .env (not committed). In Cloud Run they come from Secret Manager.
    load_dotenv()
    try:
        output = orchestrator.get_recommended_hospitals(EXAMPLE_LAT, EXAMPLE_LON)
    except RuntimeError as error:
        print(f"Could not get recommendations: {error}", file=sys.stderr)
        sys.exit(1)
    print(output)
