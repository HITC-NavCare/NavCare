import sys

import orchestrator

# Example location: downtown Montréal.
EXAMPLE_LAT = 45.5017
EXAMPLE_LON = -73.5673

if __name__ == "__main__":
    try:
        output = orchestrator.get_recommended_hospitals(EXAMPLE_LAT, EXAMPLE_LON)
    except RuntimeError as error:
        print(f"Could not get recommendations: {error}", file=sys.stderr)
        sys.exit(1)
    print(output)
