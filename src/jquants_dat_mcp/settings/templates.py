"""HTML template generators for the /settings Web UI."""

from __future__ import annotations

import html

_VALID_PLANS = ("free", "light", "standard", "premium")


def html_page(title: str, body: str) -> str:
    """Wrap body content in a minimal HTML page."""
    escaped_title = html.escape(title)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escaped_title} \u2014 jquants-dat-mcp</title>
  <style>
    body {{ font-family: sans-serif; max-width: 480px; margin: 40px auto; padding: 0 16px; color: #333; }}
    h1 {{ font-size: 1.4rem; }}
    label {{ display: block; margin-top: 12px; font-weight: bold; }}
    input[type=password], select {{ width: 100%; padding: 8px; margin-top: 4px; box-sizing: border-box; border: 1px solid #ccc; border-radius: 4px; }}
    button {{ margin-top: 16px; padding: 10px 20px; background: #0066cc; color: white; border: none; border-radius: 4px; cursor: pointer; }}
    button:hover {{ background: #0052a3; }}
    button.danger {{ background: #cc2200; }}
    button.danger:hover {{ background: #a31b00; }}
    .status {{ margin: 12px 0; padding: 10px; background: #f0f0f0; border-radius: 4px; }}
    .success {{ background: #d4edda; color: #155724; padding: 10px; border-radius: 4px; margin: 12px 0; }}
    .error {{ background: #f8d7da; color: #721c24; padding: 10px; border-radius: 4px; margin: 12px 0; }}
  </style>
</head>
<body>
{body}
</body>
</html>"""


def login_page_html(google_client_id: str) -> str:
    """Generate Google Sign-In login page HTML."""
    escaped_cid = html.escape(google_client_id)
    body = f"""<h1>J-Quants API Key Settings</h1>
<p>Please sign in with your Google account to manage your API key.</p>
<div id="g_id_onload"
     data-client_id="{escaped_cid}"
     data-callback="onSignIn"
     data-auto_prompt="false">
</div>
<div class="g_id_signin" data-type="standard" data-size="large"></div>
<script src="https://accounts.google.com/gsi/client" async defer></script>
<script>
function onSignIn(response) {{
  fetch('/settings/verify', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{credential: response.credential}})
  }}).then(r => {{ if(r.ok) window.location.reload(); }});
}}
</script>"""
    return html_page("Sign In", body)


def form_html(
    registered_plan: str | None, csrf_token: str = "", *, user_email: str | None = None
) -> str:
    """Generate the /settings API key registration form page."""
    csrf_field = f'<input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}">'
    user_info_html = (
        f'<p style="color:#555;font-size:0.9rem">Logged in as: {html.escape(user_email)}</p>'
        if user_email
        else ""
    )
    if registered_plan is not None:
        status_html = (
            f'<div class="status">Currently registered \u2014 Plan: '
            f"<strong>{html.escape(registered_plan)}</strong></div>"
        )
        button_label = "Update API Key"
        delete_section = (
            '<hr style="margin-top:24px">'
            '<form method="post" action="/settings/delete"'
            " onsubmit=\"return confirm('Delete your registered API key?')\">"
            f"{csrf_field}"
            '<button type="submit" class="danger">Delete API Key</button>'
            "</form>"
        )
    else:
        status_html = '<div class="status">No API key registered yet.</div>'
        button_label = "Register API Key"
        delete_section = ""

    plan_options = "\n".join(
        f'  <option value="{p}"{" selected" if p == (registered_plan or "free") else ""}>'
        f"{p}</option>"
        for p in _VALID_PLANS
    )

    body = f"""<h1>J-Quants API Key Settings</h1>
{user_info_html}
{status_html}
<form method="post" action="/settings">
  {csrf_field}
  <label for="api_key">J-Quants API Key
    (<a href="https://jpx-jquants.com/dashboard/api-keys" target="_blank" rel="noopener">confirm your key here</a>)
  </label>
  <input type="password" id="api_key" name="api_key" required autocomplete="off"
         placeholder="Enter your J-Quants API Key">
  <label for="plan">Plan</label>
  <select id="plan" name="plan">
{plan_options}
  </select>
  <button type="submit">{html.escape(button_label)}</button>
</form>
{delete_section}"""
    return html_page("API Key Settings", body)
