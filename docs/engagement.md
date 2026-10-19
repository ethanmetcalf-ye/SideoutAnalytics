# Statement of Work

## Sideout Analytics NCAA Women's Volleyball

*Portfolio sample; the engagement and stakeholders described here are fictional.*

### Purpose and scope

This two-month prototype will provide a public website for NCAA women's
volleyball readers, writers, and Sideout Analytics analysts. It will ingest
public match data, show statistics and team ratings, offer match predictions,
simulate the tournament bracket, and provide a chat-rate-limited AI assistant
for natural-language analysis. The goal is to assess the value of a full custom
implementation and reduce analysts' manual data work.

The prototype excludes men's volleyball and other divisions, native mobile
apps, custom data sources or analytical models, user accounts, guaranteed
uptime, and editorial news content. Any additions require agreement by both
parties.

### Deliverables and acceptance

Milestones are demonstrated on the dates below. Within five business days,
fictional client approver Jordan Rivera will accept in writing or identify
unmet checks. Each check is measurable:

| Milestone | Acceptance check |
| --- | --- |
| Data pipeline | Poll without manual triggering; display each result within 15 minutes after the first successful poll that returns it. Retain raw responses and record the successful poll and display times; show last-update time and stale status. |
| Ratings model | On all eligible completed matches in a chronological 2025 holdout, achieve a lower Brier score than a baseline that predicts each team's pre-match season win percentage. Report the sample size and both scores; if the model's score is not lower, this check fails and the report states so. |
| Public frontend and predictions | Load the site without login; show statistics and generate a prediction at least 30 minutes before every scheduled match for which schedule and input data are available. |
| Tournament launch | Simulate the bracket within five minutes after bracket data is available to the site. |
| AI assistant | Before prompt or model tuning, agree with the client approver on and freeze a 20-question evaluation set and scoring rubric. Keep the set separate from development; score at least 18 questions correctly, restrict queries to curated views, show the query used, and abstain when the data cannot answer. |
| Evaluation and handover | Deliver the backtest results, evaluation method and limitations, plus setup and operational documentation. |

### Timeline

- **Data pipeline:** Demo November 6, 2026
- **Ratings model:** Demo November 15, 2026
- **Public frontend and predictions:** Demo November 22, 2026
- **Tournament launch:** Demo November 29, 2026
- **AI assistant:** Demo December 11, 2026
- **Evaluation and handover:** Deliver December 20, 2026

### People and payment

Ethan is project manager, architect, and developer. Jordan Rivera (fictional),
Sideout Analytics Director of Volleyball Analytics, is the client approver.

No development fee is due for the prototype. Estimated hosting and model-call
costs are approximately $40 per month, paid by the client; actual costs vary
with usage and provider pricing. Future licensing or implementation requires a
separate agreement. Changes beyond this scope will be assessed and billed only
by mutual agreement.

### Key risks and mitigations

- **Unofficial API availability and terms:** Review terms before use, cache raw
  responses, show data freshness, and self-host the source where permitted.
  If use is not permitted or the source is unavailable, agree on the resulting
  scope and schedule changes.
- **Tight schedule and single-person dependency:** Prioritize the analytical
  backbone and keep setup, operations, and implementation notes current for
  handover.
- **Incorrect AI answers:** Limit the assistant to curated views, show its
  query, gate changes on the 20-question evaluation set, and abstain when
  unsupported.
