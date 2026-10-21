# app/

Application code. Nothing is implemented yet; each folder below is a placeholder.

| Path | What goes here |
| --- | --- |
| `main.py` (not created yet) | Entry point: the REST API that receives the user's latitude/longitude and returns the top 4 hospitals. |
| `integrations/` | One module per external source: `weather.py` (WeatherAPI), `travel_time.py` (Google Maps Routes API), `er_conditions.py` (Québec ER data). |
| `recommender_v1/` | v1, the rule-based microservice. Ranks hospitals with a fixed scoring formula. |
| `agent_v2/` | v2, the AI agent. Uses a Gemini model on Vertex AI to decide which tools or services to call and how to rank hospitals. |

v1 and v2 must expose the same API, so a client cannot tell which one served a request.
