"""Who wrote a message, as distinct from whose mailbox it arrived from.

One question, asked in two places that must never disagree: Stage 1 uses it to decide how a
message is triaged, and the read views use it to decide whether the records read out of that
message are evidence of a delivery or a request for one.

**The distinction this module exists to protect is origin sender vs envelope sender.** Every
message in Premier's mailbox is forwarded, so the envelope sender says `premierpm.com` on a
warehouse receiving report and on an internal expediting spreadsheet alike. Measured on the live
store, eleven of the ninety-four postable records are Atlas receiving reports forwarded by Premier
staff — `Fw: CIRCUIT OF THE AMERICAS - RR 211798-7`, `Fw: [External] CATE's CREEK - RR 211355-11`.
Reading the envelope would hold back the very documents the system exists to post, and it would
look like the rule working.

`email_log.origin_sender` is the resolved value, set by `stage1_triage` from the oldest hop of the
thread, and it is populated on every row in the store.
"""
from typing import Optional

from config import settings


def authored_internally(sender_address: Optional[str]) -> bool:
    """True where the message was *written* by Premier, not merely forwarded by them.

    Callers must pass the **origin** sender — `email_log.origin_sender`, or
    `TriagedEmail.origin_sender_address` — never the envelope `sender`. There is no way for this
    function to tell which it was handed, which is why every call site says so at the call.

    An empty address is not internal. A message whose origin could not be resolved is a message we
    know nothing about, and treating unknown as internal would silently withhold it.
    """
    domain = (sender_address or "").rsplit("@", 1)[-1].strip().lower()
    return bool(domain) and domain in {d.lower() for d in settings.INTERNAL_DOMAINS}
