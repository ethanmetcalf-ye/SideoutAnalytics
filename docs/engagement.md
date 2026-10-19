# Statement of Work

## Sideout Analytics NCAA Women's Volleyball

*Portfolio sample; the engagement and stakeholders described here are fictional.*

### Abstract

This Statement of Work (SOW) outlines the objectives, scope,
deliverables, and timelines for the development and implementation of a new
NCAA Volleyball analytics system for Sideout Analytics. The project will provide a
webapp that drives Sideout Analytics' evaluation of NCAA women's volleyball. The webapp
will provide features to visualize, query, and analyze game statistics at varying levels
of granularity. In addition, an AI assistant will be added to assist in semantic-based
ad-hoc analysis. The project will use a public API, polling at a regular schedule.
The timeline for completion is two months, with key milestones and deliverables
identified at each phase. There is no development fee for this prototype; it is
limited to the features set out in this document. Estimated operating costs and
any ongoing licensing or run-and-improve budget will be revisited after the
December prototype.

### Value

The project serves as a lean prototype to show the system's core capabilities applied 
to Sideout Analytics business requirements. As such, the prototype will provide the basic 
value of automated data ingestion, processing, visualization, and analytics -- previously
manual tasks handled by analysts. The tool will free their time for analytical application of
their skillsets to drive business value.

Any potential changes or additional requirements will be assessed and billed separately,
subject to agreement by both parties.

### Scope

The scope of the project is limited to a prototype that demonstrates whether
Sideout Analytics can gain business value from a full, custom implementation. The
prototype includes data ingestion and processing, visualization of NCAA women's
volleyball statistics, basic analytics, win prediction, and an AI assistant for
semantic-based ad-hoc analysis of the available data.

The following are out of scope for this prototype:

- A public-facing website or a publication platform for writers and readers
- Account management, user authentication, or role-based access
- Coverage of men's volleyball or divisions other than NCAA women's volleyball
- A native mobile application
- Custom data sources or custom analytical models
- Guaranteed uptime or service-level commitments
- News coverage or editorial narrative content

### Intended users

The prototype is for Sideout Analytics analysts evaluating NCAA women's
volleyball. It is intended to help analysts view, query, and analyze game
statistics and explore win predictions; it is not intended as a public-facing
product for external writers or readers.

### Deliverables and acceptance

Each milestone will be demonstrated or delivered to Sideout Analytics on the
date listed in the timeline. A milestone meets its completion check when
Sideout Analytics can observe the stated result in the demonstration or the
specified report is delivered. Within five business days of each demonstration
or delivery, the client approver will provide written acceptance or identify
which completion checks remain unmet. Data polling will run on the configured
schedule without manual triggering; the prototype does not guarantee a maximum
delay between a match and its data becoming available because publication
timing depends on the API. The predictor evaluation is an assessment of
observed results; no minimum accuracy threshold is promised by this prototype.

| Milestone | Completion check |
| --- | --- |
| Data ingestion and processing pipeline | Demonstrate the API being polled on the configured schedule without manual triggering, and successfully retrieved data being processed and made available to the prototype. |
| Ratings model | Demonstrate ratings generated for NCAA women's volleyball teams using data available to the prototype. |
| Frontend and predictions | Demonstrate the webapp displaying available statistics and a win prediction. |
| Tournament launch | Make the analytical prototype available for internal evaluation during the tournament selection-to-first-play window, subject to the data-source dependency described under Risks. |
| AI assistant | Demonstrate the assistant responding to natural-language questions using the data available to the prototype. |
| Evaluation of predictor and handover | Deliver a report describing the evaluation approach, data considered, observed prediction results, and limitations, together with available setup and operational documentation. |

### Payment

No development fee is due for the initial prototype delivery. Estimated operating costs
for hosting and model calls are approximately $40 per month and will be treated as client
costs. Actual costs may vary based on usage and provider pricing. A payment schedule for
licensing and a custom implementation will be agreed on in the case that a full
implementation is negotiated. No licensing or implementation work after the prototype is
included or authorized by this SOW; it requires a separate agreement.

### People

**Ethan:** Project manager, architect, and developer guiding the prototype. All client communication will be handled by him.

**Jordan Rivera (fictional):** Sideout Analytics Director of Volleyball Analytics and authorized client approver for milestone deliverables.

Deliverables are accepted upon written confirmation from the client approver that the
applicable completion check has been met.

### Timeline

- **Data Ingestion and Processing Pipeline:** Demo on November 6, 2026
- **Ratings Model:** Demo on November 15, 2026
- **Frontend and Predictions:** Demo on November 22, 2026
- **Tournament Launch:** Demo on November 29, 2026 (hardest deadline due to NCAA tournament selection to first-play window)
- **AI Assistant:** Demo on December 11, 2026
- **Evaluation of Predictor and Handover:** Report and handover materials delivered by December 20, 2026

### Risks

| Risk | Mitigation |
| --- | --- |
| The project depends on an open-source, unofficial API. If it becomes unavailable or changes, live data polling may stop or be disrupted; the API's terms may also restrict the intended use. | Before relying on the API, review and document whether its terms permit the intended project use. If use is not permitted or the API becomes unavailable, notify the client and agree on any alternative and resulting scope or schedule changes. Monitor ingestion and make clear when displayed data is not current. |
| The prototype timeline is tight, and the analytical backbone is needed by tournament selection. | Prioritize the ingestion, processing, ratings, and prediction milestones needed for tournament use. Raise schedule risks promptly so both parties can review priorities and impacts. |
| One teammate is responsible for implementation, creating a single-person dependency and concentration of project knowledge. | Keep essential setup, operational, and implementation notes current so project status and the work completed are easier to hand over if needed. |
| The AI assistant may return inaccurate or unsupported answers, which could undermine trust in the analysis. | Treat assistant responses as exploratory analysis, communicate that outputs require analyst verification, and do not present them as authoritative conclusions. |
