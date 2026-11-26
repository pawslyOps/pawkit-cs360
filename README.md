# Pawkit CS360

Internal customer-service page for Pawkit, served at https://cs360.pawkit.sg (GitHub Pages from `docs/`).

- `~/.config/pawkit-cs360/config.json` (private, never in this public repo): roster copied from the Zoho Sheet schedule tracker, shift codes, leave, complaint summaries.
- `build.py RAW_JSON`: packages the Zoho CRM snapshot and the config, then encrypts them into `docs/data.js`
  (PBKDF2-SHA256 + AES-256-GCM). The page decrypts them in the browser with the team passcode.
- The passcode lives only in `~/.config/pawkit-cs360/passcode` on the publishing Mac. Never commit it.
- `raw/` (the unencrypted snapshot) is git-ignored.

## Changing the passcode
1. Put the new passcode in `~/.config/pawkit-cs360/passcode` (use a generated one: five random words or 16+ random characters).
2. Run `python3 build.py raw/crm.json` (without `--no-bundle`), then commit and push `main`, so the bundled snapshot uses it too.
3. The next run re-encrypts the live data. People are asked for the new passcode.

Anything already published under an old passcode stays readable with that passcode: GitHub keeps superseded data commits reachable by ID. After a suspected leak, delete and recreate the repository, then publish under the new passcode.

## Time off requests and approval

- Staff send a request from the Time Off tab (Zoho webform "CS360 Leave Request" into the CRM module `Leave_Requests`, shown in Zoho as "Time Off Requests").
- Zoho workflow rule "CS360 time off request email" emails the approver using the template "CS360 Time Off Request". The email has Approve and Reject buttons that link to `https://cs360.pawkit.sg/#td/<request id>/<A|R>/<approval key>`.
- The page shows a confirm step; confirming posts the Zoho webform "CS360 Time Off Decision" into the module `Time_Off_Decisions` (administrators only).
- `refresh.py` applies the latest decision to a request that Zoho still shows as Pending, but only when the decision carries the approval key. The key lives in the private config as `_approval_key` (keys starting with `_` are never published) and in the Zoho email template. To change it, change both.
- A status set by hand in Zoho (Approved, Rejected, Cancelled) always wins over an email decision.
- Sick leave needs a medical document: the request form uploads it through the webform's File Upload field (`theFile`), and it lands under Attachments on the request in Zoho. `refresh.py` only publishes whether a file is attached, never the file.
- When a decision is made, Zoho workflow rule "CS360 time off decision email" emails the team member (template "CS360 Time Off Decision"). It has one condition per person (name + approval key); CRM users are emailed through their Zoho account, people without one through a typed address. Add a condition when someone joins. The name, type and dates travel in the approver's link after the key.
- Approved leave from Payboy (pawkitpteltd.payboy.biz, dashboard calendar feed `/events/leaves`, read month by month) is copied by hand into the private config as `_payboy_leave` {synced, rows}; `build.py` lists those rows with the page's own requests, marked "Payboy". It is a copy, not a live link: copy again to pick up later leave.
- Complaints show the Zoho record owner (there is no separate assignee field in the Complaints module).
- The page is titled Pawkit Ops360 (the address stays cs360.pawkit.sg). Schedule and Time Off are sub-tabs of one Workforce tab; `#schedule` and `#leave` still open them.
- WhatsApp "Reply status right now": `refresh.py` reads the last 48 hours of WOZTELL logs, groups them by customer and publishes counts plus the waiting list (name, number, since when). No message text is published. Names missing from the log are looked up in the Customers module by mobile number.
- Overview is four compact cards (Workforce, Customer Service, Services and Bookings, Orders and Inventory). Each number opens its tab. Quick links to Zoho and other tools come from `links` in the private config (so the addresses are only in the encrypted data); complaint numbers and task names link to their Zoho records.

## Where new pet owners come from (Bookings tab)

- `refresh.py` reads the Pet Owner module (`Customers_1`) once an hour for owners added since the start of the oldest month the Bookings tab can show: only the `Source` field, the created time and the last 7 characters of the record id (the same short id the bookings carry, so an owner's bookings can be matched). No name, number or email is published.
- The Source picklist holds two kinds of answer. "How they reached us" is how the record was made: App Booking, Website, WhatsApp, Cognito Forms, Manual Input, Email, Microchip Form, Euthanasia Form, or blank. Everything else (Google Search, Google Ads, TikTok, Instagram, Facebook, referrals, Existing Customer, Other) counts as "How they found us". To change the first list without a code change, set `source_via` (a list of Source values) in the private config.
- Two older picklist values come back from Zoho under their stored names and are shown as `Instagram` and `Facebook` (`Instagram Social`, `Facebook Social`). The `Ad Source` field is not used.
- "Booked" counts owners with a real booking (not a new enquiry, not fulfilled or archived, not cancelled) made on or after the day they were added; "Booking value" adds up those bookings at base price. The card follows the Day / Week / Month bar.
- Marketing answers started on 20 Nov 2026, so earlier periods show only "How they reached us".

## CS Connect (Customer Service > CS Connect)

- Notes and actions from the weekly CS meetings, under their own passcode (separate from the team passcode).
- The notes live only in `~/.config/pawkit-cs360/csconnect.json`; the passcode only in `~/.config/pawkit-cs360/csconnect-passcode`. Never commit either.
- To publish or update: edit `csconnect.json` (newest meeting shown first), run `python3 -I csconnect.py`, then commit and push `docs/csconnect.js` on `main`.
- The page keeps the CS Connect passcode for the browser tab only; Lock clears it.
