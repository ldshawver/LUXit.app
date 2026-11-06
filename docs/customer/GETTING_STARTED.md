# Getting started with LUXit

A walk-through for a new business, from signing up to leaving.

## 1. Create your account

1. Go to **Sign up** (`/auth/signup`) and enter your name, business email,
   password and business name. Accept the Terms.
2. Open the verification email and click the link (valid 48 hours). You can't
   sign in until your email is verified. **Resend** is on the sign-in page (once every 2 minutes).
3. Sign in. You land in your new workspace on the **Free plan**: 1 user,
   250 contacts, no AI features, no phone/SMS.

## 2. Set up your business

* **Onboarding** (`/onboarding`): business details, logo (PNG/JPG/GIF/WebP, up
  to 5 MB), industry, time zone.
* **Integrations** (`/settings/integrations`): connect Google Contacts to
  import your address book.

## 3. Choose a plan

**Billing** (`/app/billing`), owners and admins only:

| | Starter | Professional |
|---|---|---|
| Users | 5 (add more per user) | unlimited |
| Contacts included | 2,500 | 50,000 |
| Over the allowance | billed per contact per month | same |
| AI features | yes | yes |

Click **Subscribe** to pay securely with Stripe. Your plan is active as soon
as payment completes. **Manage billing & invoices** opens the Stripe portal to change
card, download invoices, add users or cancel.

If a payment fails you keep full access for 14 days while you update your card.

## 4. Invite your team

**Team** (`/user/manage-users`) → **Invite**: enter an email and a role.

| Role | Can |
|---|---|
| Owner | everything, including billing and closing the workspace |
| Admin | everything except closing the workspace |
| Manager / Editor | work with contacts, deals, campaigns, automations |
| Staff | same access level as Viewer |
| Inbox only | the shared inbox and phone app only, no CRM |
| Viewer | read only |

Invitations are single-use and expire after 7 days. Pending invitations
count toward your user limit. Every role change and invitation is recorded in the
security audit log, visible to owners and admins.

## 5. Add your contacts

* **Contacts** → **Import** (`/contacts/import`): a CSV file or an iPhone/iCloud vCard (.vcf) export with names, email and phone. Editors and above.
* Or add contacts one by one, or sync Google Contacts.
* **Segments** (`/segments`) group contacts for campaigns. Segment membership
  is not SMS consent: only contacts who opted in receive marketing texts, and
  STOP is always honored.

## 6. Texting and calling

Texting and calling need a phone/SMS service on your workspace. At launch,
connect your own Twilio account. Ask support until the in-app connect flow is
available. Then:

* **Inbox** and **LUX Connect** (phone app) for two-way texts and calls.
* **SMS campaigns** (`/app/sms-campaigns`): pick a sender number and an
  audience, preview who will receive it (opted-out and unconsented contacts
  are excluded), then send or schedule.

## 7. Automations

**Automations** (`/automations`): start from a template
(`/automation-templates`) or create one; set its trigger and steps. Use
**Test** before turning it on. You can pause or resume any automation.

## 8. Your account

* **Change password**: your other devices, including the LUX Connect app,
  are signed out.
* **Forgot password**: use the link on the sign-in page.
* **Export your data**: Billing → **Your data** → *Export workspace data* (owners
  and admins). You get a ZIP of everything your workspace holds. Passwords and
  connected-account credentials are never included.

## 9. Cancelling and leaving

1. **Cancel the subscription**: Billing → Manage billing → Cancel. You keep
   access until the end of the paid period.
2. **Export your data** (above).
3. **Close the workspace** (owner): Billing → *Close this workspace*, type
   the workspace name. Everyone is signed out, phone/SMS stops, pending
   invitations are cancelled.
4. Your data is kept for **30 days** in case you change your mind (contact
   support to reopen), then it is permanently deleted.

Leaving a workspace you don't own removes only your access. The last owner
can't leave: transfer ownership or close the workspace instead.

## Help

Email **support@luxit.app**. If you lost access to your email and are the
owner, support will verify your identity using your billing details before
restoring access.
