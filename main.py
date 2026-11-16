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
        if len(sys.argv) > 1 and sys.argv[1] == "v2":
            # python main.py v2 my 6-year-old has a high fever
            message = " ".join(sys.argv[2:]) or None
            output = orchestrator.get_agent_recommended_hospitals(
                EXAMPLE_LAT, EXAMPLE_LON, message
            )
        else:
            output = orchestrator.get_recommended_hospitals(EXAMPLE_LAT, EXAMPLE_LON)
    except RuntimeError as error:
        print(f"Could not get recommendations: {error}", file=sys.stderr)
        sys.exit(1)
    print(output)
