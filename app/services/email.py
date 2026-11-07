"""Outbound email via Resend.

Only one email type exists today: the audit report sent to a candidate after
the sandbox finishes. Both branches (pass and fail) send the same template
with different content — no separate handling by outcome.

Delivery model: fire-and-forget. `send()` returns False on failure but the
caller in workers/tasks.py doesn't check the return value, so a bounce or
rate-limit is invisible to the pipeline. The submission still gets marked
PASSED, the audit still gets pushed to GitHub, and the candidate just never
sees the email. Worth knowing before this becomes a real support issue.
"""

import markdown
import resend

from app.core.config import settings


# Module-level initialisation. Resend's SDK uses a module-global api_key
# rather than an instance, so this must run before any send. Setting it here
# means importing this module has a side effect — importing it in a test
# without the env var set will not fail, but the first send will. Acceptable
# trade-off: the alternative is wrapping every call in `resend.api_key = ...`.
resend.api_key = settings.resend_api_key


# CSS inlined into the email HTML. Kept as a module constant so a designer
# can edit it without touching the send logic. Most of these rules exist
# because some email clients (Outlook especially) strip or ignore <style>
# tags — inline styles would be more reliable, but markdown.markdown() emits
# its own HTML that we can't easily inline styles onto after the fact.
_EMAIL_STYLES = """
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px; }
    h1, h2, h3 { color: #111; }
    .report-container { background-color: #f9f9f9; padding: 20px; border-radius: 8px; border: 1px solid #eaeaea; }
    pre { background-color: #1e1e1e; color: #d4d4d4; padding: 15px; border-radius: 6px; overflow-x: auto; font-family: 'Courier New', Courier, monospace; font-size: 14px; }
    code { font-family: 'Courier New', Courier, monospace; background-color: #f1f1f1; padding: 2px 4px; border-radius: 4px; }
    pre code { background-color: transparent; padding: 0; }
    .footer { margin-top: 30px; font-size: 12px; color: #888; text-align: center; border-top: 1px solid #eaeaea; padding-top: 20px; }
"""


def send_report_email(to_email: str, report_markdown: str) -> bool:
    try:
        # markdown.markdown() passes raw HTML through untouched — it does NOT
        # sanitise. Safety here depends entirely on report.py html.escape()ing
        # the log content before interpolation. If you ever change report.py
        # or call this function with markdown that wasn't produced by
        # generate_markdown_report(), candidate stdout could carry live HTML
        # into the recipient's mail client. A bleach.clean() pass on
        # html_content would be belt-and-braces — see TODO.
        html_content = markdown.markdown(
            report_markdown, extensions=["fenced_code", "tables"]
        )

        template = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <style>{_EMAIL_STYLES}</style>
        </head>
        <body>
            <div class="report-container">
                {html_content}
            </div>
            <div class="footer">
                Verification Verification Engine<br>
                <em>Evidence-anchored technical hiring.</em>
            </div>
        </body>
        </html>
        """

        params = {
            "from": settings.resend_from_email,
            "to": [to_email],
            "subject": "Your Verification Audit Report",
            "html": template,
            # Plain-text fallback. Some clients (corporate Outlook, some
            # mobile), and every terminal-based email client, prefer or
            # require this. Resend delivers both parts as a multipart
            # message; the client picks. Omitting it means a plain-text
            # client shows a blank body.
            "text": report_markdown,
        }

        # resend.Emails.send() is synchronous — it does a blocking HTTP POST.
        # In the Celery worker this runs inside asyncio.run() (see
        # workers/tasks.py), so it briefly blocks the event loop. Harmless
        # here because the worker handles one task at a time and there are
        # no concurrent coroutines competing for the loop. Would matter if
        # the worker ever grew concurrent task handling.
        resend.Emails.send(params)
        print(f"Email sent to {to_email}")
        return True

    except Exception as e:
        # Broad catch is intentional: the caller only cares about success vs
        # failure. But the return value is discarded in tasks.py, so a failure
        # here is *silent* — the candidate just doesn't get the email, and
        # nothing in the DB records that. See TODO below.
        print(f"Failed to send email: {e}")
        return False


# Deferred on purpose, not forgotten:
#
# - Return value unused. tasks.py calls send_report_email(...) and ignores the
#   bool. A failed send leaves no audit trail: submission still marked PASSED,
#   report still pushed to GitHub, email never delivered, nothing recorded.
#   Fix requires either (a) logging the failure into the submissions row via
#   a new rejection_reason-ish field, or (b) letting the task retry on
#   failure — but that brings the next problem into sharp relief:
#
# - No idempotency. Celery's max_retries=3 means a task can run the email
#   send up to 3 times. If the first attempt actually delivered (but the
#   HTTP response was lost, or the client timed out after delivery), the
#   candidate receives 2-3 copies of the same report. Proper fix is an
#   idempotency key on the Resend request — Resend supports one via the
#   `headers` param — keyed on submission_id. Deferred because at current
#   volume, duplicate emails are annoying but not damaging.
#
# - No reply_to. The report tells candidates "reply to dispute", but the
#   `from` address is noreply-style and replies go nowhere. Add
#   "reply_to": settings.resend_from_email (or a dedicated support address)
#   once one exists. This is the kind of thing a reviewer notices immediately
#   when they think "could I actually use this?".
#
# - No List-Unsubscribe header. Bulk senders (which is what Verification will look
#   like to Gmail/Yahoo once it sends >100/day) require one to avoid spam
#   classification. Currently irrelevant — send volume is tiny — but the day
#   it isn't, the fix isn't obvious from the code. See RFC 8058.
#
# - No bounce handling. Resend returns a message ID from send(); we discard
#   it. Bounces and complaints arrive via a separate webhook that isn't wired
#   up. Without it, we have no way to know if the candidate ever received the
#   email.
#
# - Sender name is bare. "noreply@verification.dev" in the From: header shows as
#   just that in most clients; "Verification <noreply@verification.dev>" renders as
#   "Verification" with the address hidden until click. Trivial change, but tied
#   to whatever format settings.resend_from_email currently uses — worth
#   aligning both when Resend's domain verification is finalised.
#
# - Subject line is static. "Your Verification Audit Report" hides the outcome.
#   A candidate who passed their first challenge can't tell from the inbox
#   whether it's a pass or fail — which is arguably an intentional design
#   choice (don't celebrate a fail in the subject), but worth revisiting.