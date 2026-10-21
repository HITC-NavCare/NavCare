# Course requirements checklist

Summary of the COEN424/6313 Fall 2026 project description (Option A: our own service).
The lecturer's PDF is the authority; check it if anything here is unclear.
This page only lists what must be true at the end; tasks will be tracked as GitHub issues.

## Mandatory

- [ ] Runs on a cloud platform and uses a platform-specific feature (Google Cloud: Cloud Run, Firestore, Vertex AI).
- [ ] Data model designed and documented.
- [ ] NoSQL data store used for application data (Firestore).
- [ ] REST API defined with an OpenAPI spec on SwaggerHub.
- [ ] v1 (rule-based microservice) and v2 (AI agent) of the recommender, exposing the **same API**.
- [ ] v2 uses a model to decide how to handle a request (for example, choosing and calling tools). Passing v1's output through a model to reword it does not count.
- [ ] Strangler-pattern facade in front of v1/v2 that decides which version serves each request.
- [ ] Budget with alert thresholds set **before** the first deployment and first load test.
- [ ] Resources labelled so v1 and v2 costs can be separated; model usage (tokens or calls per request) recorded.
- [ ] Cost per request and total cost reported for v1 and v2.
- [ ] Model API keys stored in Secret Manager, never in code or the repository.
- [ ] Source code and reports shared with the lecturer and PoD.
- [ ] AI-assisted development documented with traceable prompts (`docs/ai-usage/`).

## Canary release

- [ ] Criteria table written **before** the canary starts, with columns: metric, how measured, threshold, action.
  It must cover at least: availability (error rate incl. timeouts), p95 latency,
  correctness/data consistency (checked against v1 or against rules), and cost per request.
- [ ] At least three traffic steps before full promotion (for example 10%, 25%, 50%, then 100%), same load and fixed duration at each step.
- [ ] Any violated criterion sends all traffic back to v1.
- [ ] Metrics collected with Cloud Monitoring (or Prometheus/Grafana) and shown on a dashboard.
- [ ] Final demo shows at least one promotion step and one rollback.

## Extra feature (choose one)

Pending team decision. Options: (1) REST/Open APIs on SwaggerHub, (2) Docker and/or DevOps CI/CD,
(3) performance and operation measurement.

## Deliverables

| ID | When | What |
| --- | --- | --- |
| D3 | Week 6 Thursday | 3-minute progress talk; slides `D3-[GroupID].pdf` by 23:55. Must include architecture, cost/budget so far, v1/v2 status and the canary criteria table. |
| D4 | Week 12 Thursday | 15-minute presentation and live demo (backup video); IEEE-format report of 5-8 pages `D4-report-[GroupID].pdf`; slides `D4-presentation-[GroupID]`; source package `D4-project-[GroupID].zip`. |

## Pending decisions

- Extra feature (see above).
- Facade mechanism, for example Cloud Run traffic splitting between revisions or an API gateway.
- Canary thresholds and step durations.
- Gemini model version for v2.
