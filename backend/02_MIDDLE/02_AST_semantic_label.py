"""
02_AST_semantic_label.py — Phase 2, Step 2: Semantic Labeling

Assigns ontology labels to each AST node using:
  1. Title-based pattern matching (all document types)
  2. Text-content pattern matching (signature phrases in section body)
  3. GPT-4o-mini fallback (structured output with Pydantic)

Usage:
    python 02_AST_semantic_label.py --file_name "Complaint (Epic Games to Apple"
    python 02_AST_semantic_label.py --document_id "abc-123-uuid"
"""

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
from pydantic import BaseModel
from supabase import create_client

# Load .env from project root (two levels up from backend/02_MIDDLE/)
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))


# ===========================================================================
# ONTOLOGY LABEL SETS
# ===========================================================================

CONTRACT_LABELS = [
    # --- Document scaffolding ---
    "contract_root",
    "table_of_contents",
    "title_page",

    # --- Preamble / Opening ---
    "preamble",
    "preamble.title_block",
    "preamble.parties",
    "preamble.recitals",
    "preamble.effective_date",

    # --- Definitions ---
    "definitions",
    "definitions.term",

    # --- Scope of Work / Subject Matter ---
    "scope",
    "scope.subject_matter",
    "scope.exclusions",
    "scope.performance_standards",

    # --- Obligations ---
    "obligation",
    "obligation.performance",
    "obligation.payment",
    "obligation.delivery",
    "obligation.reporting",
    "obligation.notification",
    "obligation.compliance",

    # --- Rights ---
    "rights",
    "rights.license_grant",
    "rights.audit_rights",
    "rights.step_in_rights",

    # --- Conditions ---
    "condition",
    "condition.precedent",
    "condition.subsequent",
    "condition.concurrent",

    # --- Representations & Warranties ---
    "representation",
    "representation.authority",
    "representation.compliance",
    "representation.financial",
    "representation.no_litigation",
    "warranty",
    "warranty.product_quality",
    "warranty.service_level",
    "warranty.ip_ownership",

    # --- Restrictive Covenants ---
    "covenant",
    "covenant.non_compete",
    "covenant.non_solicitation",
    "covenant.non_disclosure",
    "covenant.exclusivity",

    # --- Indemnification & Liability ---
    "indemnification",
    "indemnification.scope",
    "indemnification.limitations",
    "indemnification.procedure",
    "liability",
    "liability.limitation",
    "liability.cap",
    "liability.exclusion",

    # --- Termination ---
    "termination",
    "termination.for_cause",
    "termination.for_convenience",
    "termination.expiration",
    "termination.effects",

    # --- Dispute Resolution ---
    "dispute_resolution",
    "dispute_resolution.governing_law",
    "dispute_resolution.jurisdiction",
    "dispute_resolution.arbitration",
    "dispute_resolution.mediation",

    # --- Confidentiality ---
    "confidentiality",
    "confidentiality.scope",
    "confidentiality.exceptions",
    "confidentiality.duration",

    # --- IP Rights ---
    "ip_rights",
    "ip_rights.ownership",
    "ip_rights.license",
    "ip_rights.assignment",

    # --- Data Protection (EU/Spanish contracts) ---
    "data_protection",
    "data_protection.processing",
    "data_protection.security",
    "data_protection.breach_notification",

    # --- Insurance ---
    "insurance",

    # --- Force Majeure ---
    "force_majeure",

    # --- General Provisions / Boilerplate ---
    "general_provisions",
    "general_provisions.amendment",
    "general_provisions.assignment",
    "general_provisions.notices",
    "general_provisions.severability",
    "general_provisions.entire_agreement",
    "general_provisions.waiver",
    "general_provisions.counterparts",

    # --- Closing ---
    "signature_block",
    "exhibit_reference",
    "schedule_reference",
    "exhibit_content",
    "schedule_content",
]

COMPLAINT_LABELS = [
    # --- Document scaffolding ---
    "complaint_root",
    "table_of_contents",

    # --- Caption ---
    "caption",
    "caption.court",
    "caption.parties",
    "caption.case_number",

    # --- Opening ---
    "introduction",
    "nature_of_action",

    # --- Parties ---
    "parties",
    "parties.plaintiff",
    "parties.defendant",
    "parties.third_party",
    "parties.related_entity",

    # --- Jurisdiction & Venue ---
    "jurisdiction",
    "jurisdiction.subject_matter",
    "jurisdiction.personal",
    "venue",

    # --- Factual Allegations ---
    "factual_allegations",
    "factual_allegations.background",
    "factual_allegations.relationship",
    "factual_allegations.key_events",
    "factual_allegations.breach_event",
    "factual_allegations.damages_narrative",
    "factual_allegations.timeline",
    "factual_allegations.concealment",

    # --- Causes of Action ---
    "causes_of_action",
    "causes_of_action.breach_of_contract",
    "causes_of_action.breach_of_fiduciary",
    "causes_of_action.negligence",
    "causes_of_action.fraud",
    "causes_of_action.statutory_violation",
    "causes_of_action.unjust_enrichment",
    "causes_of_action.tortious_interference",
    "causes_of_action.conversion",
    "causes_of_action.trade_secret",
    "causes_of_action.ip_infringement",
    "causes_of_action.antitrust",
    "causes_of_action.unfair_competition",
    "causes_of_action.consumer_protection",
    "causes_of_action.declaratory_relief",
    "causes_of_action.other",

    # --- Damages ---
    "damages",
    "damages.compensatory",
    "damages.consequential",
    "damages.punitive",
    "damages.statutory",
    "damages.equitable_relief",
    "damages.injunctive_relief",
    "damages.attorneys_fees",

    # --- Relief ---
    "prayer_for_relief",

    # --- Procedural / Closing ---
    "jury_demand",
    "verification",
    "conditions_precedent",
    "certificate_of_service",

    # --- Answer / Response sections ---
    "admissions_denials",
    "affirmative_defense",
    "counterclaim",
    "crossclaim",

    # --- Closing ---
    "signature_block",
    "exhibit_reference",
    "exhibit_content",
]

# ===========================================================================
# Add these to the ONTOLOGY LABEL SETS section (after MOTION_BRIEF_LABELS)
# ===========================================================================

DISCOVERY_LABELS = [
    # --- Document scaffolding ---
    "discovery_root",
    "table_of_contents",

    # --- Caption ---
    "caption",
    "caption.court",
    "caption.parties",
    "caption.case_number",

    # --- Preamble / Instructions ---
    "preamble",                             # Opening paragraph (who serves what on whom)
    "definitions",                          # Definitions section (common in interrogatories)
    "instructions",                         # Instructions to responding party
    "preliminary_statement",                # Preliminary statement / objections boilerplate

    # --- Interrogatories ---
    "interrogatory",                        # Individual interrogatory (numbered question)
    "interrogatory.answer",                 # Answer to an interrogatory
    "interrogatory.objection",              # Objection to an interrogatory

    # --- Requests for Production ---
    "request_for_production",               # Individual RFP (numbered demand)
    "request_for_production.response",      # Response to an RFP
    "request_for_production.objection",     # Objection to an RFP

    # --- Requests for Admission ---
    "request_for_admission",                # Individual RFA (numbered statement)
    "request_for_admission.response",       # Admit / deny / qualified response
    "request_for_admission.objection",      # Objection to an RFA

    # --- Subpoena ---
    "subpoena.command",                     # The subpoena's command (produce / testify / both)
    "subpoena.schedule",                    # Schedule of documents / categories requested
    "subpoena.return_date",                # Date/time/place for compliance

    # --- Deposition ---
    "deposition.cover",                     # Cover page (witness name, date, location, court reporter)
    "deposition.appearances",               # Attorneys present and their roles
    "deposition.examination",               # Examination section header
    "deposition.direct",                    # Direct examination (Q&A by noticing party)
    "deposition.cross",                     # Cross-examination (Q&A by opposing party)
    "deposition.redirect",                  # Redirect examination
    "deposition.exhibit_marking",           # Discussion of exhibit markings during deposition
    "deposition.stipulations",              # Stipulations between counsel on the record
    "deposition.certification",             # Court reporter certification

    # --- General Discovery ---
    "meet_and_confer",                      # Meet-and-confer statement / correspondence
    "privilege_log",                        # Privilege log entries
    "discovery_schedule",                   # Discovery plan / scheduling order references

    # --- Closing ---
    "verification",                         # Verification under oath (required for interrogatories)
    "certificate_of_service",
    "signature_block",
    "exhibit_reference",
    "exhibit_content",
]

COURT_ORDER_LABELS = [
    # --- Document scaffolding ---
    "order_root",
    "table_of_contents",

    # --- Caption ---
    "caption",
    "caption.court",
    "caption.parties",
    "caption.case_number",

    # --- Opening ---
    "introduction",                         # Opening paragraph identifying what's before the court
    "procedural_posture",                   # What motions/issues are pending

    # --- Background ---
    "factual_background",                   # Court's summary of the facts
    "procedural_history",                   # Procedural history of the case

    # --- Legal Standard ---
    "legal_standard",                       # Standard the court applies
    "legal_standard.review",                # Standard of review (if appellate opinion)
    "legal_standard.governing_rule",        # Rule/test being applied

    # --- Analysis (the core of the opinion) ---
    "analysis",                             # "ANALYSIS" or "DISCUSSION" section header
    "analysis.issue",                       # Analysis of a specific issue (numbered I, II, III)
    "analysis.sub_issue",                   # Sub-issue within a main issue
    "analysis.jurisdiction",                # Analysis of jurisdiction
    "analysis.standing",                    # Analysis of standing
    "analysis.merits",                      # Analysis on the merits
    "analysis.damages",                     # Analysis of damages
    "analysis.injunction",                  # Analysis of injunctive relief factors
    "analysis.privilege",                   # Analysis of privilege claims
    "analysis.discovery",                   # Analysis of discovery disputes
    "analysis.sanctions",                   # Analysis of sanctions
    "analysis.summary_judgment",            # Analysis for summary judgment motion
    "analysis.dismissal",                   # Analysis for motion to dismiss

    # --- Holdings & Conclusions ---
    "holding",                              # Court's holding / conclusion of law
    "finding_of_fact",                      # Court's factual findings (bench trial / evidentiary hearing)

    # --- Order / Judgment ---
    "order",                                # "IT IS HEREBY ORDERED" — the actual commands
    "order.granted",                        # Motion granted
    "order.denied",                         # Motion denied
    "order.granted_in_part",                # Motion granted in part, denied in part
    "order.scheduling",                     # Scheduling directives (deadlines, hearing dates)
    "order.sanctions",                      # Sanctions imposed
    "judgment",                             # Final judgment

    # --- Concurrence / Dissent (appellate opinions) ---
    "concurrence",                          # Concurring opinion
    "dissent",                              # Dissenting opinion

    # --- Closing ---
    "signature_block",
    "certificate_of_service",
    "exhibit_reference",
    "exhibit_content",
]


MOTION_BRIEF_LABELS = [
    # --- Document scaffolding ---
    "motion_root",
    "table_of_contents",
    "table_of_authorities",
    "index_of_exhibits",

    # --- Caption / Cover ---
    "caption",
    "caption.court",
    "caption.parties",
    "caption.case_number",

    # --- Opening ---
    "introduction",
    "statement_of_issues",

    # --- Facts & History ---
    "statement_of_facts",
    "statement_of_facts.background",
    "statement_of_facts.key_events",
    "statement_of_facts.relationship",
    "procedural_history",

    # --- Jurisdiction & Standing ---
    "jurisdiction",
    "jurisdiction.subject_matter",
    "jurisdiction.appellate",
    "standing",

    # --- Legal Standard ---
    "legal_standard",
    "legal_standard.review",
    "legal_standard.governing_rule",

    # --- Argument ---
    "argument",
    "argument.main",
    "argument.sub",
    "argument.likelihood_of_success",
    "argument.irreparable_harm",
    "argument.balance_of_equities",
    "argument.public_interest",
    "argument.legal_error",
    "argument.factual_error",
    "argument.abuse_of_discretion",
    "argument.statutory_interpretation",
    "argument.policy",

    # --- Conclusion ---
    "conclusion",
    "prayer_for_relief",

    # --- Compliance & Procedural ---
    "compliance_statement",
    "consent_statement",
    "certificate_of_service",

    # --- Closing ---
    "signature_block",
    "exhibit_reference",
    "exhibit_content",
]

FINANCIAL_LABELS = [
    "financial_root", "cover_page", "management_discussion", "auditor_report",
    "balance_sheet", "income_statement", "cash_flow_statement", "equity_statement",
    "notes_to_financials", "notes.accounting_policies", "notes.revenue_recognition",
    "notes.debt_obligations", "notes.contingencies", "notes.related_party",
    "supplementary_schedules", "signature_block",
]

ANNUAL_REPORT_LABELS = [
    "annual_report_root", "letter_to_shareholders", "company_overview",
    "business_segments", "risk_factors", "legal_proceedings", "executive_compensation",
    "corporate_governance", "financial_statements", "market_data", "appendices",
]


# ===========================================================================
# ONTOLOGY SELECTION
# ===========================================================================

def _select_ontology(document_type: str | None) -> tuple[list[str], str]:
    """Return (label_list, ontology_name) based on document_type from Phase 1."""
    dt = (document_type or "").strip()

    if dt.startswith("Contract"):
        return CONTRACT_LABELS, "Contract"

    # Motions, briefs, appeals
    if any(k in dt for k in (
        "Appeal", "Brief", "Motion", "Memorandum",
        "Opposition", "Reply Brief", "Petition",
        "Application", "Emergency",
    )):
        return MOTION_BRIEF_LABELS, "Motion / Brief"

    # Complaints, answers, initiating pleadings
    if dt.startswith("Pleading") or any(k in dt for k in (
        "Complaint", "Answer", "Counterclaim", "Cross-Claim",
    )):
        return COMPLAINT_LABELS, "Pleading / Legal Complaint"

    # Court orders, opinions, judgments
    if any(k in dt for k in (
        "Opinion", "Order", "Judgment", "Ruling",
        "Decision", "Decree", "Minute Order",
        "Administrative", "Case Summary",
    )):
        return COURT_ORDER_LABELS, "Court Order / Opinion"

    # Discovery documents
    if any(k in dt for k in (
        "Interrogator", "Request for Production", "Request for Admission",
        "Subpoena", "Deposition", "Discovery", "RFP", "RFA",
        "Document Request", "Privilege Log",
    )):
        return DISCOVERY_LABELS, "Discovery"

    if any(k in dt for k in ("Financial", "10-K", "10-Q")):
        return FINANCIAL_LABELS, "Financial Statement"

    if "Annual Report" in dt:
        return ANNUAL_REPORT_LABELS, "Annual Report"

    return [], "Unknown"


# ===========================================================================
# TITLE-BASED PATTERN MATCHING (per ontology)
# ===========================================================================

# ===========================================================================
# Add these to the PATTERN MATCHING section (after _ANNUAL_PATTERNS)
# ===========================================================================

_DISCOVERY_PATTERNS: list[tuple[list[str], str]] = [
    # Scaffolding
    (["Table of Contents", "Contents"],                                 "table_of_contents"),
    (["Caption"],                                                       "caption"),

    # Preamble / instructions
    (["Definitions"],                                                   "definitions"),
    (["Instructions", "General Instructions"],                          "instructions"),
    (["Preliminary Statement", "Preliminary Objections"],               "preliminary_statement"),

    # Interrogatories
    (["Interrogator"],                                                  "interrogatory"),
    (["Answer to Interrogator"],                                        "interrogatory.answer"),

    # Requests for Production
    (["Request for Production", "Requests for Production",
      "Document Request", "RFP"],                                       "request_for_production"),

    # Requests for Admission
    (["Request for Admission", "Requests for Admission", "RFA"],        "request_for_admission"),

    # Subpoena
    (["Subpoena", "Subpoena Duces Tecum"],                              "subpoena.command"),
    (["Schedule of Documents", "Document Schedule",
      "Schedule A", "Attachment A"],                                    "subpoena.schedule"),

    # Deposition
    (["Deposition of", "Deposition Transcript",
      "Oral Deposition"],                                               "deposition.cover"),
    (["Appearances"],                                                   "deposition.appearances"),
    (["Direct Examination"],                                            "deposition.direct"),
    (["Cross-Examination", "Cross Examination"],                        "deposition.cross"),
    (["Redirect Examination", "Redirect"],                              "deposition.redirect"),
    (["Reporter's Certification", "Court Reporter",
      "Certification of Reporter"],                                     "deposition.certification"),
    (["Stipulation"],                                                   "deposition.stipulations"),

    # General discovery
    (["Meet and Confer", "Meet-and-Confer"],                            "meet_and_confer"),
    (["Privilege Log"],                                                 "privilege_log"),

    # Closing
    (["Verification"],                                                  "verification"),
    (["Certificate of Service", "Proof of Service"],                    "certificate_of_service"),
    (["Signature", "Respectfully Submitted", "Dated:"],                 "signature_block"),
    (["Exhibit"],                                                       "exhibit_content"),
]

_COURT_ORDER_PATTERNS: list[tuple[list[str], str]] = [
    # Scaffolding
    (["Table of Contents", "Contents"],                                 "table_of_contents"),
    (["Caption"],                                                       "caption"),

    # Opening
    (["Introduction"],                                                  "introduction"),
    (["Procedural Posture", "Procedural Background",
      "Procedural History"],                                            "procedural_posture"),

    # Background
    (["Factual Background", "Background", "Statement of Facts",
      "Facts"],                                                         "factual_background"),

    # Legal Standard
    (["Standard of Review", "Legal Standard",
      "Applicable Standard", "Applicable Law"],                         "legal_standard"),

    # Analysis
    (["Analysis", "Discussion"],                                        "analysis"),
    (["Jurisdiction"],                                                  "analysis.jurisdiction"),
    (["Standing"],                                                      "analysis.standing"),
    (["Merits"],                                                        "analysis.merits"),
    (["Damages"],                                                       "analysis.damages"),
    (["Injunctive Relief", "Injunction",
      "Preliminary Injunction", "Temporary Restraining"],               "analysis.injunction"),
    (["Privilege"],                                                     "analysis.privilege"),
    (["Discovery"],                                                     "analysis.discovery"),
    (["Sanction"],                                                      "analysis.sanctions"),
    (["Summary Judgment"],                                              "analysis.summary_judgment"),
    (["Motion to Dismiss", "Dismissal"],                                "analysis.dismissal"),

    # Holdings
    (["Holding", "Conclusion of Law", "Conclusions of Law"],            "holding"),
    (["Finding of Fact", "Findings of Fact"],                           "finding_of_fact"),

    # Order / Judgment
    (["IT IS HEREBY ORDERED", "IT IS SO ORDERED",
      "Order", "Ruling"],                                               "order"),
    (["Judgment", "Final Judgment"],                                    "judgment"),

    # Appellate
    (["Concurrence", "Concurring Opinion"],                             "concurrence"),
    (["Dissent", "Dissenting Opinion"],                                 "dissent"),

    # Closing
    (["Signature", "Dated:", "So Ordered"],                             "signature_block"),
    (["Certificate of Service", "Proof of Service"],                    "certificate_of_service"),
    (["Exhibit"],                                                       "exhibit_content"),
]

_CONTRACT_PATTERNS: list[tuple[list[str], str]] = [
    # Preamble / opening
    (["Title Page", "Cover Page"],                                      "title_page"),
    (["Table of Contents", "Contents"],                                 "table_of_contents"),
    (["WHEREAS", "Recitals", "Background"],                             "preamble.recitals"),
    (["Definitions", "Defined Terms"],                                  "definitions"),

    # Core commercial terms
    (["Scope of Work", "Scope of Services", "Statement of Work"],       "scope.subject_matter"),
    (["Service Level", "SLA", "Performance Standard", "KPI"],           "scope.performance_standards"),
    (["Payment", "Fees", "Compensation", "Pricing", "Consideration"],   "obligation.payment"),
    (["Delivery", "Milestones", "Timeline"],                            "obligation.delivery"),
    (["Reporting", "Reports"],                                          "obligation.reporting"),

    # Protective clauses
    (["Representation", "Representations and Warranties"],              "representation"),
    (["Warrant"],                                                       "warranty"),
    (["Indemnif"],                                                      "indemnification"),
    (["Limitation of Liability", "Liability"],                          "liability.limitation"),
    (["Non-Compete", "Non Compete", "Noncompete"],                      "covenant.non_compete"),
    (["Non-Solicitation", "Non Solicitation"],                          "covenant.non_solicitation"),
    (["Non-Disclosure", "NDA", "Nondisclosure"],                        "covenant.non_disclosure"),
    (["Exclusivity"],                                                   "covenant.exclusivity"),
    (["Confidential"],                                                  "confidentiality"),

    # IP and data
    (["Intellectual Property", "IP Rights", "Ownership of Work"],       "ip_rights"),
    (["Data Protection", "Data Privacy", "GDPR", "Personal Data"],      "data_protection"),

    # Termination and disputes
    (["Termination", "Term and Termination"],                           "termination"),
    (["Governing Law", "Applicable Law", "Choice of Law"],              "dispute_resolution.governing_law"),
    (["Jurisdiction", "Forum", "Venue"],                                "dispute_resolution.jurisdiction"),
    (["Arbitration"],                                                   "dispute_resolution.arbitration"),
    (["Mediation"],                                                     "dispute_resolution.mediation"),
    (["Dispute Resolution", "Dispute"],                                 "dispute_resolution"),
    (["Force Majeure"],                                                 "force_majeure"),
    (["Insurance"],                                                     "insurance"),

    # Boilerplate
    (["General Provisions", "Miscellaneous", "General Terms"],          "general_provisions"),
    (["Amendment", "Modification"],                                     "general_provisions.amendment"),
    (["Assignment", "Delegation"],                                      "general_provisions.assignment"),
    (["Notice", "Notices"],                                             "general_provisions.notices"),
    (["Severability"],                                                  "general_provisions.severability"),
    (["Entire Agreement", "Merger Clause", "Integration"],              "general_provisions.entire_agreement"),
    (["Waiver"],                                                        "general_provisions.waiver"),
    (["Counterpart"],                                                   "general_provisions.counterparts"),

    # Closing
    (["Signature", "IN WITNESS WHEREOF", "Execution"],                  "signature_block"),
    (["Exhibit", "Appendix", "Annex"],                                  "exhibit_content"),
    (["Schedule"],                                                      "schedule_content"),
]

_COMPLAINT_PATTERNS: list[tuple[list[str], str]] = [
    # Scaffolding
    (["Table of Contents", "Contents"],                                 "table_of_contents"),

    # Caption
    (["Caption"],                                                       "caption"),

    # Opening
    (["Nature of the Action", "Nature of Action",
      "Preliminary Statement", "Summary of Action"],                    "nature_of_action"),
    (["Introduction"],                                                  "introduction"),

    # Parties
    (["The Parties", "Parties"],                                        "parties"),

    # Jurisdiction
    (["Jurisdiction and Venue", "Jurisdiction"],                         "jurisdiction"),
    (["Venue"],                                                         "venue"),

    # Facts
    (["General Allegations", "Factual Allegations",
      "Statement of Facts", "Factual Background",
      "Background Facts"],                                              "factual_allegations"),

    # Causes of action — section headers
    (["Causes of Action", "Claims for Relief",
      "Cause of Action"],                                               "causes_of_action"),
    # Numbered counts / causes ("COUNT I", "FIRST CAUSE OF ACTION", etc.)
    # These are caught here so they don't fall through to GPT unlabeled.
    # The label is causes_of_action; 03B will extract the specific type from text.
    (["Count I", "Count II", "Count III", "Count IV", "Count V",
      "Count VI", "Count VII", "Count VIII", "Count IX", "Count X",
      "Count 1", "Count 2", "Count 3", "Count 4", "Count 5",
      "COUNT I", "COUNT II", "COUNT III", "COUNT IV", "COUNT V",
      "First Cause of Action", "Second Cause of Action",
      "Third Cause of Action", "Fourth Cause of Action",
      "Fifth Cause of Action", "Sixth Cause of Action",
      "FIRST CAUSE OF ACTION", "SECOND CAUSE OF ACTION",
      "THIRD CAUSE OF ACTION", "FOURTH CAUSE OF ACTION"],              "causes_of_action"),
    # Typed counts — if the title names the claim type directly
    (["Breach of Contract"],                                            "causes_of_action.breach_of_contract"),
    (["Breach of Fiduciary", "Fiduciary Duty"],                         "causes_of_action.breach_of_fiduciary"),
    (["Negligence", "Gross Negligence"],                                "causes_of_action.negligence"),
    (["Fraud", "Fraudulent Misrepresentation",
      "Fraudulent Inducement", "Fraudulent Concealment"],               "causes_of_action.fraud"),
    (["Unjust Enrichment", "Quantum Meruit"],                           "causes_of_action.unjust_enrichment"),
    (["Tortious Interference"],                                         "causes_of_action.tortious_interference"),
    (["Conversion"],                                                    "causes_of_action.conversion"),
    (["Trade Secret", "Misappropriation"],                              "causes_of_action.trade_secret"),
    (["Patent Infringement", "Trademark Infringement",
      "Copyright Infringement"],                                        "causes_of_action.ip_infringement"),
    (["Antitrust", "Sherman Act", "Clayton Act",
      "Competition"],                                                   "causes_of_action.antitrust"),
    (["Unfair Competition", "Unfair Business",
      "Competencia Desleal"],                                           "causes_of_action.unfair_competition"),
    (["Consumer Protection", "Abusive Clause",
      "Consumer Rights"],                                               "causes_of_action.consumer_protection"),
    (["Declaratory Relief", "Declaratory Judgment"],                     "causes_of_action.declaratory_relief"),

    # Damages
    (["Damages"],                                                       "damages"),

    # Relief
    (["Prayer for Relief", "Wherefore", "Relief Requested",
      "Demand for Relief"],                                             "prayer_for_relief"),

    # Procedural / Closing
    (["Jury Demand", "Demand for Jury Trial",
      "Trial by Jury"],                                                 "jury_demand"),
    (["Verification"],                                                  "verification"),
    (["Conditions Precedent"],                                          "conditions_precedent"),
    (["Certificate of Service", "Proof of Service"],                    "certificate_of_service"),

    # Answer-specific
    (["Admissions", "Denials", "Admits and Denies",
      "Response to Allegations"],                                       "admissions_denials"),
    (["Affirmative Defense"],                                           "affirmative_defense"),
    (["Counterclaim"],                                                  "counterclaim"),
    (["Cross-Claim", "Crossclaim", "Cross Claim"],                      "crossclaim"),

    # Closing
    (["Signature", "Respectfully Submitted",
      "Dated:"],                                                        "signature_block"),
    (["Exhibit"],                                                       "exhibit_content"),
]

_MOTION_BRIEF_PATTERNS: list[tuple[list[str], str]] = [
    # Scaffolding
    (["Table of Contents", "Contents"],                                 "table_of_contents"),
    (["Table of Authorities", "Authorities Cited"],                     "table_of_authorities"),
    (["Index of Exhibits", "List of Exhibits", "Exhibit Index"],        "index_of_exhibits"),
    (["Title Page", "Cover Page"],                                      "caption"),

    # Opening
    (["Introduction", "Preliminary Statement"],                         "introduction"),
    (["Questions Presented", "Issues Presented",
      "Statement of Issues", "Issues on Appeal"],                       "statement_of_issues"),

    # Facts & History
    (["Statement of Facts", "Factual Background",
      "Statement of the Case", "Background"],                           "statement_of_facts"),
    (["Procedural History", "Procedural Background",
      "Prior Proceedings"],                                             "procedural_history"),

    # Jurisdiction
    (["Jurisdiction"],                                                  "jurisdiction"),
    (["Standing"],                                                      "standing"),

    # Legal Standard
    (["Standard of Review", "Legal Standard",
      "Applicable Standard"],                                           "legal_standard"),

    # Argument — parent only; sub-arguments go to GPT
    (["Argument"],                                                      "argument"),

    # Argument sub-types (pattern-matchable when titles are explicit)
    (["Likelihood of Success", "Likely to Succeed",
      "Merits", "Probability of Success"],                              "argument.likelihood_of_success"),
    (["Irreparable Harm", "Irreparable Injury",
      "Irreparably Harmed"],                                            "argument.irreparable_harm"),
    (["Balance of Equities", "Balance of Hardships",
      "Balancing"],                                                     "argument.balance_of_equities"),
    (["Public Interest", "Harm the Public",
      "Public Harm"],                                                   "argument.public_interest"),

    # Conclusion
    (["Conclusion"],                                                    "conclusion"),
    (["Prayer for Relief", "Relief Requested",
      "Wherefore"],                                                     "prayer_for_relief"),

    # Compliance & Procedural
    (["Statement of Compliance", "Certificate of Compliance",
      "Word Count"],                                                    "compliance_statement"),
    (["Statement of Consent", "Consent or Opposition"],                 "consent_statement"),
    (["Certificate of Service", "Proof of Service"],                    "certificate_of_service"),

    # Closing
    (["Signature", "Respectfully Submitted",
      "IN WITNESS", "Dated:"],                                          "signature_block"),
    (["Exhibit", "Appendix", "Attachment"],                             "exhibit_content"),
]

_FINANCIAL_PATTERNS: list[tuple[list[str], str]] = [
    (["Balance Sheet", "Statement of Financial Position"],          "balance_sheet"),
    (["Income Statement", "Statement of Operations", "Profit and Loss"], "income_statement"),
    (["Cash Flow"],                                                 "cash_flow_statement"),
    (["Stockholders' Equity", "Changes in Equity"],                 "equity_statement"),
    (["MD&A", "Management Discussion", "Management's Discussion"],  "management_discussion"),
    (["Auditor", "Independent Registered"],                         "auditor_report"),
    (["Accounting Polic"],                                          "notes.accounting_policies"),
    (["Revenue Recognition"],                                       "notes.revenue_recognition"),
    (["Debt", "Borrowings"],                                        "notes.debt_obligations"),
    (["Contingenc"],                                                "notes.contingencies"),
    (["Related Part"],                                              "notes.related_party"),
]

_ANNUAL_PATTERNS: list[tuple[list[str], str]] = [
    (["Letter to Shareholder", "Dear Shareholder", "Message from"],  "letter_to_shareholders"),
    (["Company Overview", "About Us", "Who We Are"],                 "company_overview"),
    (["Business Segment", "Operating Segment"],                      "business_segments"),
    (["Risk Factor"],                                                "risk_factors"),
    (["Legal Proceeding"],                                           "legal_proceedings"),
    (["Executive Compensation", "Compensation Discussion"],          "executive_compensation"),
    (["Corporate Governance", "Board of Directors"],                 "corporate_governance"),
    (["Financial Statement", "Consolidated Statement"],              "financial_statements"),
    (["Market Data", "Stock Price", "Dividend"],                     "market_data"),
    (["Appendix", "Appendices", "Exhibit"],                          "appendices"),
]


# ===========================================================================
# TEXT-CONTENT PATTERN MATCHING (ontology-agnostic, second pass)
# ===========================================================================
# When the title doesn't match anything, check the first 500 chars of the
# section body for signature phrases that reliably identify section type.
# These work across all document types.

_TEXT_CONTENT_PATTERNS: list[tuple[list[str], str]] = [
    # Contract opening
    (["WHEREAS", "WITNESSETH"],                                     "preamble.recitals"),
    (["NOW, THEREFORE", "NOW THEREFORE"],                           "preamble"),
    (["IN WITNESS WHEREOF"],                                        "signature_block"),

    # Complaint / pleading
    (["WHEREFORE, plaintiff", "WHEREFORE, Plaintiff",
      "prayer for relief", "prays for judgment",
      "prays for relief"],                                          "prayer_for_relief"),
    (["hereby demand a trial by jury",
      "demands trial by jury",
      "demand for jury trial"],                                     "jury_demand"),
    (["this Court has jurisdiction",
      "jurisdiction of this Court",
      "subject matter jurisdiction"],                               "jurisdiction"),
    (["venue is proper", "venue lies in"],                          "venue"),

    # Closing / procedural
    (["I hereby certify", "served by",
      "Certificate of Service", "served upon"],                     "certificate_of_service"),
    (["respectfully submitted",
      "Respectfully Submitted"],                                    "signature_block"),

    # Motion/brief specific
    (["standard of review", "reviews de novo",
      "abuse of discretion standard",
      "clearly erroneous"],                                         "legal_standard"),

    # Add to the existing _TEXT_CONTENT_PATTERNS list:

    # Court orders
    (["IT IS HEREBY ORDERED", "IT IS SO ORDERED",
      "the Court ORDERS", "the Court hereby ORDERS"],               "order"),
    (["the Court finds", "this Court finds",
      "the Court concludes"],                                       "holding"),
    (["For the foregoing reasons",
      "for the reasons stated above",
      "for the reasons set forth"],                                 "holding"),

    # Discovery
    (["propound the following interrogatories",
      "propounds the following",
      "answer each of the following"],                              "interrogatory"),
    (["request that defendant produce",
      "requests that plaintiff produce",
      "produce the following documents"],                           "request_for_production"),
    (["admit that", "admit or deny"],                               "request_for_admission"),
    (["you are commanded to", "you are hereby commanded",
      "subpoena duces tecum"],                                      "subpoena.command"),
    (["Q.", "Q:"],                                                  "deposition.examination"),
]


def _text_content_match(text: str) -> str | None:
    """
    Second-pass pattern match: check section text body for signature phrases.
    Returns a label if a match is found, else None.
    Only checks the first 500 chars to keep it fast.
    """
    snippet = text[:500] if text else ""
    if not snippet.strip():
        return None
    # Check both original case and lowercase for each keyword
    snippet_lower = snippet.lower()
    for keywords, label in _TEXT_CONTENT_PATTERNS:
        if any(kw.lower() in snippet_lower for kw in keywords):
            return label
    return None


# ===========================================================================
# TITLE-BASED PATTERN MATCH ROUTER
# ===========================================================================

def _pattern_match(title: str, ontology_name: str) -> str | None:
    """Return label if section title matches a pattern for this ontology, else None."""
    t = title.lower()

    _ONTOLOGY_TO_PATTERNS = {
        "Contract":                  _CONTRACT_PATTERNS,
        "Motion / Brief":            _MOTION_BRIEF_PATTERNS,
        "Pleading / Legal Complaint": _COMPLAINT_PATTERNS,
        "Court Order / Opinion":     _COURT_ORDER_PATTERNS,
        "Discovery":                 _DISCOVERY_PATTERNS,
        "Financial Statement":       _FINANCIAL_PATTERNS,
        "Annual Report":             _ANNUAL_PATTERNS,
    }

    patterns = _ONTOLOGY_TO_PATTERNS.get(ontology_name, [])
    for keywords, label in patterns:
        if any(kw.lower() in t for kw in keywords):
            return label

    # Special case: financial notes
    if "notes to" in t and ("financial" in t or "consolidated" in t):
        return "notes_to_financials"

    return None



def _use_pattern_matching(ontology_name: str) -> bool:
    """All ontologies support pattern matching as the first pass."""
    return ontology_name != "Unknown"


# ===========================================================================
# GPT-4o-mini STRUCTURED OUTPUT
# ===========================================================================

class SemanticLabel(BaseModel):
    semantic_label: str
    confidence: float


def _gpt_label(
    client,
    section_title: str,
    parent_title: str | None,
    section_text: str,
    document_type: str,
    ontology_labels: list[str],
) -> tuple[str, float]:
    """
    Call GPT-4o-mini to classify a section. Returns (label, confidence).
    Retries up to 3 times with backoff.

    Adaptive snippet length: if the title is short/generic (numbered sections,
    synthetic headings), sends 3000 chars instead of 1500 so GPT has enough
    context to make a good call.
    """
    labels_str = "\n".join(f"  - {l}" for l in ontology_labels)
    system_prompt = (
        f"You are a legal document analyst. Given a section from a '{document_type}', "
        f"classify it using ONLY the following ontology labels:\n{labels_str}\n\n"
        "Return a JSON object with 'semantic_label' (string, must be exactly one label "
        "from the provided list) and 'confidence' (float 0-1).\n\n"
        "IMPORTANT: Section titles may be real headings from the document OR "
        "AI-generated topic descriptions (e.g., 'Topic: Payment Terms'). "
        "Always classify based on the section TEXT CONTENT, not just the title. "
        "If the title is vague or numbered (e.g., 'Article I', 'Section 3', 'III.'), "
        "rely entirely on the text to determine the correct label."
    )

    parent_str = parent_title if parent_title else "None (root level)"

    # Adaptive snippet length: short/generic titles get more text
    title_stripped = section_title.strip()
    title_is_informative = (
        len(title_stripped) > 10
        and not title_stripped.replace(".", "").replace(" ", "").isdigit()
        and not title_stripped.lower().startswith("topic:")
        and not title_stripped.lower().startswith("section")
        and not title_stripped.lower().startswith("article")
    )
    snippet_len = 1500 if title_is_informative else 3000
    text_snippet = (section_text or "")[:snippet_len]

    user_prompt = (
        f"Section title: {section_title}\n"
        f"Parent section title: {parent_str}\n"
        f"Section text (first {snippet_len} chars):\n{text_snippet}"
    )

    for attempt in range(4):
        try:
            response = client.beta.chat.completions.parse(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt},
                ],
                response_format=SemanticLabel,
                timeout=30,
            )
            result = response.choices[0].message.parsed
            label      = result.semantic_label
            confidence = float(result.confidence)

            if label not in ontology_labels:
                return "unrecognized", 0.0

            return label, confidence

        except Exception as e:
            err_str = str(e)
            is_rate_limit = "429" in err_str or "rate_limit" in err_str
            if attempt < 3:
                wait = (15 * (attempt + 1)) if is_rate_limit else (2 ** attempt)
                if is_rate_limit:
                    print(f"  [Rate limit] waiting {wait}s before retry {attempt + 1}/3...")
                time.sleep(wait)
            else:
                print(f"  WARNING: GPT call failed after 4 attempts — {e}")
                return "error", 0.0

    return "error", 0.0


# ===========================================================================
# SUPABASE HELPERS
# ===========================================================================

def _get_client():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("ERROR: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set in .env")
        sys.exit(1)
    return create_client(url, key)


def _resolve_document(supabase, args) -> tuple[str, str, str | None]:
    """Return (document_id, file_name, document_type)."""
    if args.document_id:
        resp = supabase.table("documents").select("id, file_name, document_type").eq("id", args.document_id).execute()
    else:
        resp = supabase.table("documents").select("id, file_name, document_type").eq("file_name", args.file_name).execute()

    if not resp.data:
        key = args.document_id or args.file_name
        print(f"ERROR: No document found for '{key}'")
        sys.exit(1)

    row = resp.data[0]
    return row["id"], row["file_name"], row.get("document_type")


# ===========================================================================
# MAIN
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(description="Assign semantic labels to AST nodes.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--document_id", help="UUID of the document in Supabase")
    group.add_argument("--file_name",   help="file_name stem of the document")
    args = parser.parse_args()

    # Check OpenAI key early
    openai_key = os.environ.get("OPENAI_API_KEY")

    supabase = _get_client()
    document_id, file_name, document_type = _resolve_document(supabase, args)

    ontology_labels, ontology_name = _select_ontology(document_type)
    use_patterns = _use_pattern_matching(ontology_name)

    if ontology_name == "Unknown":
        print(
            f"  WARNING: document_type='{document_type}' not recognized — "
            "all sections will be labeled 'unrecognized' and flagged for review."
        )

    # Fetch sections with parent titles
    try:
        resp = (
            supabase.table("sections")
            .select("id, section_title, section_text, parent_section_id, start_page")
            .eq("document_id", document_id)
            .execute()
        )
    except Exception as e:
        print(f"ERROR: Failed to fetch sections — {e}")
        sys.exit(1)

    sections = resp.data or []
    if not sections:
        print(f"ERROR: No sections found for '{file_name}'")
        sys.exit(1)

    # Build id → title map for parent lookup
    id_to_title: dict[str, str] = {
        s["id"]: (s.get("section_title") or "") for s in sections
    }

    # Sort by start_page
    sections.sort(key=lambda s: (s.get("start_page") is None, s.get("start_page") or 0))

    # Initialize OpenAI client — needed for GPT fallback on any ontology
    openai_client = None
    if openai_key:
        from openai import OpenAI
        openai_client = OpenAI()
    elif ontology_name != "Unknown":
        # Pattern matching might handle everything, but GPT fallback needs the key
        print("  WARNING: OPENAI_API_KEY not set — sections that don't pattern-match will be 'unrecognized'")

    # Label each section
    title_pattern_count = 0
    text_pattern_count  = 0
    gpt_count           = 0
    flagged_count       = 0
    error_count         = 0

    # updates dict keyed by sec_id so parallel GPT results can be merged in
    updates: dict[str, dict] = {}
    gpt_queue: list[dict] = []   # sections that need GPT after pattern passes

    # --- Pass 1: pattern matching (no I/O, instant) ---
    for sec in sections:
        sec_id       = sec["id"]
        title        = sec.get("section_title") or ""
        text         = sec.get("section_text") or ""
        parent_id    = sec.get("parent_section_id")
        parent_title = id_to_title.get(parent_id) if parent_id else None

        label      = "unrecognized"
        confidence = 0.0
        source     = "pattern"
        matched    = False

        if ontology_name == "Unknown":
            flagged_count += 1
        else:
            # TIER 1: Title-based pattern match
            if use_patterns:
                title_match = _pattern_match(title, ontology_name)
                if title_match and title_match in ontology_labels:
                    label      = title_match
                    confidence = 1.0
                    title_pattern_count += 1
                    matched = True

            # TIER 2: Text-content pattern match
            if not matched:
                text_match = _text_content_match(text)
                if text_match and text_match in ontology_labels:
                    label      = text_match
                    confidence = 0.9
                    source     = "pattern"
                    text_pattern_count += 1
                    matched = True

            # TIER 3: queue for parallel GPT
            if not matched:
                if openai_client:
                    gpt_queue.append({
                        "_sec_id":       sec_id,
                        "_title":        title,
                        "_parent_title": parent_title,
                        "_text":         text,
                    })
                    continue   # result filled in after parallel pass
                else:
                    flagged_count += 1

        updates[sec_id] = {
            "id":                  sec_id,
            "semantic_label":      label,
            "semantic_confidence": confidence,
            "label_source":        source,
        }

    # --- Pass 2: parallel individual GPT calls for unmatched sections ---
    if gpt_queue and openai_client:
        doc_type = document_type or ontology_name

        def _label_one(item: dict) -> tuple[str, str, float]:
            lbl, conf = _gpt_label(
                openai_client, item["_title"], item["_parent_title"], item["_text"],
                doc_type, ontology_labels,
            )
            return item["_sec_id"], lbl, conf

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(_label_one, item): item for item in gpt_queue}
            for f in as_completed(futures):
                sec_id, label, conf = f.result()
                gpt_count += 1
                if label in ("unrecognized", "error"):
                    error_count += 1
                elif conf < 0.7:
                    flagged_count += 1
                updates[sec_id] = {
                    "id":                  sec_id,
                    "semantic_label":      label,
                    "semantic_confidence": conf,
                    "label_source":        "gpt-4o-mini",
                }

    updates_list = list(updates.values())

    # Write back to Supabase — 4 parallel workers with retry on connection errors
    def _update_one(sec_id: str, payload: dict):
        for attempt in range(3):
            try:
                supabase.table("sections").update(payload).eq("id", sec_id).execute()
                return None
            except Exception as e:
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                else:
                    return f"Section {sec_id}: {e}"

    write_errors = 0
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(_update_one, upd.pop("id"), upd)
            for upd in updates_list
        ]
        for f in as_completed(futures):
            err = f.result()
            if err:
                print(f"  WARNING: {err}")
                write_errors += 1

    if write_errors:
        print(
            f"ERROR: Labeling completed but {write_errors} section(s) failed to write "
            f"for '{file_name}'."
        )
        sys.exit(1)

    total_pattern = title_pattern_count + text_pattern_count
    print(
        f"SUCCESS: Labeled {len(sections)} sections for '{file_name}'. "
        f"{total_pattern} pattern-matched ({title_pattern_count} title, {text_pattern_count} text), "
        f"{gpt_count} GPT-labeled, "
        f"{flagged_count} flagged for review, "
        f"{error_count} errors."
    )


if __name__ == "__main__":
    main()