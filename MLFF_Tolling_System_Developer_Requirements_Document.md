MLFF Tolling System – Developer Requirements Document
Scope: Field (Gantry/Edge) + Plaza Control Center + Back-Office Integrations (NETC/TMCC/Vahan) Standards Baseline: IHMCL/NHAI/MoRTH/NPCI + BIS/ARAI/IRC/STQC/CERT-In Version: 1.0 | Date: March 2026
1. Purpose and Outcome
1.1 Business Objective
Implement Multi-Lane Free Flow (MLFF) tolling with barrier-free operations at highway speeds (100–120 km/h) using RFID (FASTag) + ANPR + LiDAR/Radar and real-time NETC transactions, including violations & e-Notice workflows.
1.2 System Goals
Seamless tolling across multiple lanes simultaneously
<2 seconds end-to-end transaction processing (detection → NETC post)
99.9% availability monthly
Automated evidence capture + audit trail + reporting
Compliance and certification readiness (STQC, CERT-In, etc.
2. In-Scope and Out-of-Scope
2.1 In-Scope
Multi-sensor event fusion (RFID + ANPR + LiDAR/Radar)
Vehicle classification (7+ classes)
NETC integration (validation, posting, reconciliation support)
Violation detection + e-Notice generation and management
Control-center dashboard, monitoring, reporting
Security controls (RBAC, audit logs, encryption, integrity)
2.2 Out-of-Scope (unless separately approved)
GNSS distance-based toll charging engine (future)
Smartphone OBU as primary tolling mechanism (future)
Law-enforcement backend system development (only integration hooks)
3. System Context and Architecture Requirements
3.1 Three-Tier Logical Architecture
Tier 1 – Field/Gantry (Edge):
Sensors: RFID, ANPR (front+rear), LiDAR/Radar, IR illuminators, audit cameras
Edge compute: Lane/Roadside Controller
Local buffering for offline operation
Outdoor protection: IP66/IP67 where applicable
Tier 2 – Plaza/Control Center:
MLFF application server(s), DB, monitoring consoles
Network (core/edge switches), firewall, UPS
Operator UI and video wall feeds
Tier 3 – Back Office:
NETC CCH integration, TMCC integration
Bank acquirer interfaces for settlement/reconciliation
Long-term storage, analytics, violation portal (if required)
3.2 Data Flow (Mandatory)
Vehicle detection → ID capture (RFID + ANPR) → classification → transaction creation → NETC validation/post → settlement status → violation/notice (if needed) → audit storage.
4. Functional Requirements (FR)
4.1 Vehicle Event Detection and Correlation
FR-001: System shall create a Vehicle Pass Event (VPE) upon detection from LiDAR/Radar for every vehicle crossing gantry.
Acceptance: VPE generated with timestamp, lane id, direction, sensor references.
FR-002: System shall correlate RFID reads and ANPR reads to the same VPE within ≤100 ms correlation window (configurable).
Acceptance: Correlation confidence score stored; mismatches flagged.
FR-003: System shall support simultaneous multi-lane processing up to 12 lanes per site configuration.
Acceptance: No cross-lane mis-association above SLA thresholds.
4.2 RFID (FASTag) Processing
FR-010: System shall ingest RFID reads (EPC/Tag ID, RSSI, antenna port, timestamp). FR-011: System shall determine “confirmed tag read” within ≤500 ms of VPE creation if tag present. FR-012: System shall enforce transaction uniqueness and prevent double charging for the same VPE.
Acceptance: Duplicate prevention logic demonstrated in test cases.
FR-013: System shall support blacklist/hotlist checks as provided through NETC response flows (as applicable in ICD/PG). FR-014: If RFID read fails, system shall fall back to ANPR-based identification for violation workflow.
4.3 ANPR Capture and OCR
FR-020: System shall capture front and rear images per lane per VPE. FR-021: System shall run OCR and output a normalized VRN with confidence score and plate crop image. FR-022: System shall support fuzzy matching and partial reads for exception handling. FR-023: System shall store evidence images and metadata for minimum retention period (see Section 6).
4.4 Vehicle Classification
FR-030: System shall classify vehicles into 7+ classes aligned to NH Fee Rules / CMVR mapping (site-configurable). FR-031: System shall produce classification confidence score and measured dimensions/axle-related features (as per sensor capability). FR-032: System shall detect mismatch between tag class (if available) and observed class, and route to exception/violation workflow based on rules.
4.5 Toll Transaction Creation and NETC Integration
FR-040: System shall create a toll transaction object per VPE containing:
Site/plaza ID, gantry ID, lane ID, direction
Timestamp synchronized to IST (NTP)
Vehicle class, tag id (if any), VRN (if any), evidence references
Amount, tariff rule id, and exception flags
FR-041: System shall integrate with NPCI NETC over HTTPS using mutual TLS and required auth tokens/keys. FR-042: System shall support message formats JSON/XML as required by NETC ICD. FR-043: System shall implement retry + queuing if NETC is unreachable, with eventual reconciliation.
Acceptance: Offline queue tested; no data loss; posting resumes automatically.
4.6 Violation Detection and e-Notice
FR-050: System shall identify and flag violations at minimum:
Tag-less vehicle (no valid RFID)
Invalid/closed/unregistered tag (as per NETC response / rules)
Low balance/insufficient funds (as available through NETC outcomes)
Class mismatch requiring enforcement (rule-based)
FR-051: System shall generate an e-Notice record within 24 hours of violation detection. FR-052: e-Notice shall attach evidence bundle:
Front image, rear image, overview image
Optional video snippet (see FR-060)
Sensor metadata and timestamps
Audit hash
FR-053: System shall support a manual review queue for operator approval/rejection before final notice (configurable). FR-054: System shall expose integration endpoints to State Transport/Police systems (export/push/pull as agreed).
4.7 Audit Trail, Evidence, and Video Snippets
FR-060: System shall store 5 seconds before and 5 seconds after the VPE time as video snippet per violation (and optionally per all VPEs if storage allows). FR-061: System shall maintain tamper-evident logs using hash chaining or equivalent integrity mechanism. FR-062: System shall log all sensor inputs used for decisioning (RFID reads, OCR outputs, classification outputs).
4.8 Operations Dashboard and Reporting
FR-070: Dashboard shall show in real time:
Traffic count (lane-wise, class-wise)
Revenue totals and transaction outcomes
Equipment status health (RFID/ANPR/LiDAR/Radar/network)
Alerts/alarms and SLA KPI widgets
FR-071: Reports shall be available:
Daily/weekly/monthly collection statements
Class distribution, peak hour traffic
Equipment uptime and SLA compliance
Violation summary and status aging
FR-072: Export formats: CSV + PDF (minimum), with signed audit footer optional.
4.9 User Roles and Access Control
FR-080: System shall implement RBAC roles:
IHMCL/NHAI Admin
Acquirer Bank user
Plaza Manager
Auditor (read-only)
Operator (review workflow)
Public User (violation check/payment portal, if implemented)
FR-081: MFA shall be supported for privileged roles. FR-082: All user actions shall be audited (who/what/when/from where).
4.10 TMCC Integration
FR-090: System shall push site KPIs/health to TMCC every 5 minutes (configurable). FR-091: Protocol shall be secure WebSocket or HTTPS POST with mutual auth and retry logic.
4.11 Vahan Integration
FR-100: System shall integrate with MoRTH Vahan API for VRN validation for violation workflows where permitted. FR-101: If Vahan unavailable, system shall queue lookups and continue notice workflow with status “pending verification.”
5. Non-Functional Requirements (NFR)
5.1 Performance
NFR-001: End-to-end transaction processing time < 2 seconds (detection → NETC post attempt). NFR-002: Correlation latency <100 ms (sensor fusion). NFR-003: Support peak loads as per site traffic with headroom (target: ≥2x peak observed during SAT).
5.2 Availability and Resilience
NFR-010: System availability 99.9% monthly (equipment + application). NFR-011: Dual WAN + local buffering; no transaction loss during WAN outage. NFR-012: N+1 server redundancy at control center.
5.3 Security (CERT-In aligned)
NFR-020: TLS 1.2+ mandatory; TLS 1.3 preferred for internal services. NFR-021: Network segmentation via VLANs for management/data/video. NFR-022: Annual security audit + vulnerability remediation tracking. NFR-023: Incident reporting workflow compatible with CERT-In 6-hour reporting requirement (process + logs).
5.4 Data Retention and Privacy
NFR-030: Retain transactions, images, audit logs minimum 5 years. NFR-031: Masking/anonymization for analytics (VRN masking) where applicable. NFR-032: Access to personal data restricted and logged.
5.5 Time Sync
NFR-040: All components synchronized via NTP to IST with ±1 second accuracy.
6. Data Requirements
6.1 Core Entities (Minimum)
Vehicle Pass Event (VPE)
SensorRead (RFIDRead, ANPRRead, ClassifierRead)
Transaction (NETC request/response mapping)
Violation
eNotice
EquipmentHealth
UserActionAudit
6.2 Evidence Storage
Images: front/rear/overview with metadata
Video snippets: indexed by VPE/Violation
Hash/signature metadata per record group
7. Interfaces and APIs (Developer Deliverables)
7.1 Internal APIs (Mandatory)
/vpe/create, /vpe/update
/fusion/correlate
/transaction/create, /transaction/post-netc, /transaction/status
/violation/create, /enotice/generate, /enotice/review
/health/heartbeat, /health/alerts
/reports/*
7.2 External Integrations (Mandatory)
NETC CCH APIs (as per ICD 2.5 / PG 4.0+)
TMCC push endpoint
Vahan API (if credentials provided)
Bank reconciliation import/export
8. Logging, Monitoring, and Alerting
LOG-001: Centralized logs from edge + plaza servers with retention policy aligned to audit requirements. LOG-002: Alerts: sensor down, read-rate below threshold, OCR drop, classification mismatch spikes, WAN down, disk nearing capacity. LOG-003: SLA metrics computed daily and monthly with penalty-ready summaries.
9. Testing and Acceptance Requirements
9.1 FAT (Factory Acceptance Test) – Developer Support
RFID read rate, multi-tag inventory, dense reader mode validation
ANPR OCR accuracy tests (India dataset)
Classifier validation across 7 classes
Security baseline scan results
9.2 SAT (Site Acceptance Test) – Must Pass
7-day live traffic run demonstrating:
RFID read rate >99%
ANPR day >95%, night >92%
Classification >99%
No crashes / unplanned downtime
Reconciliation checks against sampled ground truth
9.3 STQC Readiness Package (Developer Deliverable)
Architecture diagrams, data flow, threat model summary
Test reports, logs, versioning, build provenance
Role matrix, access control proofs, audit logs
10. Deployment Requirements
DEP-001: Support on-prem deployment at plaza with optional back-office/cloud components. DEP-002: Configuration-driven lane/gantry mapping; no hardcoding lane counts. DEP-003: Blue/green or rolling updates for application servers with rollback. DEP-004: Edge buffering must survive power/network interruptions (UPS window + local persistence).
11. Definition of Done (DoD) for Developers
A feature is “done” only when:
Unit tests + integration tests exist
Logs + metrics emitted
RBAC enforced for any UI/API access
Audit entry created for critical actions
Failure/retry behavior validated (NETC/TMCC outages)
Evidence storage + retention policies applied
SAT KPI acceptance criteria mapped and testable
12. Developer Backlog Starter (Suggested Epics)
Sensor Ingestion Layer (RFID/ANPR/LiDAR/Radar)
VPE Engine + Correlation/Fusion
Classification Engine + Rules
NETC Adapter (mTLS, retries, queuing, idempotency)
Violation + eNotice Workflow + Review Queue
Evidence Store (images/video) + Integrity Hashing
Dashboard + Alerts + SLA KPI Calculator
Reporting + Export
RBAC + MFA + Audit Logs
TMCC/Vahan Integrations
Deployment/Observability (CI/CD, monitoring, backups)