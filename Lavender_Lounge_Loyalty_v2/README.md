# Lavender Lounge Loyalty — v2

## Customer account
Each customer enters:
- Full name
- Phone number
- Email address

The email address is the unique loyalty-account identifier.

## Automatic emails from Lavender Lounge Gmail
The application sends:
- Verification code email
- Welcome email for new customers
- Points-earned email after a purchase
- Points-adjustment email
- Reward-redemption email
- Manual staff email from the admin customer page

## Gmail setup
Use a Google App Password rather than the normal Gmail password.

Set these environment variables:

```text
GMAIL_ADDRESS=your-lavender-address@gmail.com
GMAIL_APP_PASSWORD=your-google-app-password
```

If Gmail is not configured, the application prints outgoing emails in the terminal so you can still test locally.

## Run locally

```bash
pip install -r requirements.txt
python app.py
```

Customer site:
http://127.0.0.1:5000

Admin:
http://127.0.0.1:5000/admin/login

Default local admin password:
lavender-admin

Before public deployment:
- change ADMIN_PASSWORD
- change SECRET_KEY
- use HTTPS
- add login-code rate limiting
- add GDPR/privacy information and marketing consent
- use a production database and backups
