# Changelog

All notable changes to `ppm-receiver-automation` are recorded here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries are added in the pull request that makes the change, not retrospectively.

---

## [Unreleased]

### Added
- Governance and operating documentation required by Ashford GitHub Repository and Developer
  Standards v1.5 §6: `CLAUDE.md`, `CONTRIBUTING.md`, `SECURITY.md`, `.github/CODEOWNERS`,
  `.github/pull_request_template.md`, and `docs/` covering architecture, deployment, rollback,
  support, data handling and the Spitfire integration.
- `docs/testing-evidence/` for the reproducible evidence Critical tier requires.
- **A person can overrule triage about whether a message is a delivery notification, in both
  directions.** Mail set aside as not-a-delivery has moved off `/ui/manual` onto its own page,
  `/ui/not-deliveries`, listing the rule that set each message aside and the person who disagreed.
  A message on Needs a human can be flagged *Not a delivery* and leaves the queue; a message on the
  new page can be flagged *This is a delivery* and returns to the queue, where the existing
  Create-a-record form makes the record. Both decisions are signed, carry a reason, and are
  reversible. Nothing is extracted or staged automatically by either. The reason is a click:
  the form offers the common ones — no PO or delivery data, advertising, internal mail, a scheduled
  report, an order confirmation, a carrier status update, a calendar invite — and pressing them
  writes into the same box, so two reasons are two clauses and an unanticipated one is still typed.
- `mail_overrides` table and `pipeline/mail_overrides.py`, holding one current verdict per message.
  Kept out of `email_log` because that table is written `INSERT OR REPLACE` over a fixed column
  list, which would erase a human decision on the next reprocess. Created by
  `state_db.get_connection` like every other table here — no migration to run. It **is** in
  `_EMAIL_KEYED_TABLES`, so `tools/reprocess_mail.py` clears and reports it: reprocessing usually
  means a rule was fixed, which is exactly when a stale override would go on hiding real mail.

- **`rule_5e_bulk_mail_noise` — advertising is suppressed at triage instead of queued.** The
  external twin of `rule_5c_internal_noise`: same guards, but qualified by a property of the message
  rather than the sender's domain, so it keeps working when next month's marketing arrives from a
  new brand. A message is set aside only when all four hold — the body carries the opt-out block
  commercial mail is obliged to provide; it names no purchase order; it carries no readable
  attachment and no photograph; and no hop in the thread claims goods arrived. Fail any one and it
  is routed to a person exactly as before. Measured on the live store: of the messages whose body is
  available, 11 move and every one is advertising, none of the mail that produced a record or was
  read as a delivery moves, and the rule can only ever take mail that would have been
  `rule_7_unknown`.
- **A lexical classifier for advertising, derived from measuring all 1,961 messages.**
  `pipeline/parsing/promotional.py` holds two weighted term lists as data — promotional signals and
  business signals — with the measured rates behind every weight in its docstring. A message is
  called advertising only on a clear promotional score **and a business score of exactly zero**: one
  business signal (a purchase order, a reply, a property name, an attachment) is an absolute veto,
  never a subtraction. Measured against the live store: 51 of the 453 messages on the queue are
  advertising, 41 more are contested, and **0 of 833 known delivery mails** is called advertising.
  Three signals that read like marketing tells were measured and rejected — ALL-CAPS subjects
  (Premier's own shout), outbound link count (delivery mail carries more), and tracking pixels.
- `rule_5f_possible_advertising` — promotional in shape but contested, or not clear enough. **Routed
  to a person, not hidden**, and badged **Maybe advertising** on Needs a human so the reason chip
  filters to exactly that decision. Deliberately not in `NOT_A_DELIVERY_RULES`: it is a request to
  look, not a verdict.
- `tools/mail_lexicon_report.py` — re-runs the whole measurement and writes the evidence to
  `dev_reports/mail_lexicon_analysis.html`: which features separate the two populations, which only
  look as though they do, every term with its weight, and the false-positive check.
- `tools/preview_bulk_mail_rule.py` — read-only, dry-run by default. Re-reads mail already in the
  store, runs the real ladder over it, and reports what the new rule would set aside and which guard
  stopped it on the rest. Writes nothing; it is not `tools/reprocess_mail.py` and says so.

### Changed
- `README.md` rewritten to meet §6: ownership, systems touched, data classification, local setup,
  testing, deployment, support and rollback.
- `.env.example` completed — every configuration variable the code reads is now documented with a
  placeholder. Previously eleven were undocumented.
- `.gitignore` widened to the whole `state/` tree and to generated reports.
- The "needs a human" count now includes mail a person has called a delivery and that carries no
  record yet, so the sidebar badge and the header stat both move when a verdict is set. It is work,
  and it is counted as work.

### Security
- Removed a real production hostname that was shipping as a default value in `.env.example`.

---

## Notes on the first release

This project was developed in a Conscious Creations repository before migrating to the Ashford
organisation. That history is not carried forward into this repository — it contained client
operational data that cannot be committed under §9 and §12, and sanitising a working tree does not
sanitise history.

The prior repository is retained privately as the development record and archived per §11, so the
Ashford repository is the authoritative source.
