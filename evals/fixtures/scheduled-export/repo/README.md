# Example scheduled export

The `exports/upload` function runs every six hours and sends the latest summary file to a partner's upload endpoint. It authenticates with the API token in the Modal secret `partner-upload`, which reaches the function as `PARTNER_TOKEN`.

Partner tokens expire after 90 days. To renew one, the owner creates a new token in the partner's portal and replaces the secret's value; no code change is needed. Each call uploads the latest file in full, so rerunning the function after a failure is safe.
