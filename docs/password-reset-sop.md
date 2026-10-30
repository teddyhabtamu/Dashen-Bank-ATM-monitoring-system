# Password Recovery SOP (portal-local accounts)

Until the AD cutover, forgotten portal passwords are recovered by **admin
reset** — there is no email self-service yet (it needs proven SMTP first).

## Procedure

1. The user contacts an administrator **out of band** (phone/Teams — never
   accept a reset request by email alone; the mailbox may be the compromised
   part).
2. Admin opens **User Management → Edit** on that account and sets a new
   temporary password meeting the on-screen policy (min 10 chars, all four
   classes).
3. Saving **re-arms forced change**: at next login the user lands on
   **My Account** and cannot navigate elsewhere until they set their own
   password (logout stays available).
4. Share the temporary password through a **second channel** (call it out,
   don't email it to the same mailbox you just verified by email).
5. Confirm in **Audit Log**: one `USER_EDIT` (admin) followed by that user's
   `PASSWORD_CHANGE`. No `LOGIN_FAILED` burst around the request (which
   would suggest guessing, not forgetting).

## Notes

- Break-glass `admin` is exempt from forced change and must never be
  shared as a fallback login.
- Admins cannot see existing passwords (scrypt hashes only) — reset, don't
  "remind".
- After AD cutover, directory accounts reset via bank IAM; this SOP then
  covers local break-glass accounts only.
