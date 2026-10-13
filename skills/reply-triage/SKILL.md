---
name: reply-triage
description: Read a contact's reply to a follow-up and decide the single next step.
metadata:
  purposes: confirm_after_reply
  channels: email
---
# Reply triage

1. If the reply picks one of the offered options, confirm exactly that option
   with purpose `confirm_after_reply`, and do not wait for another reply.
2. If the reply asks for something outside the mission facts (price, special
   terms), do not answer it; end the segment and say a coordinator must take
   over.
3. If the reply says to stop, propose nothing.
4. You may suggest a memory about this contact (scope "contact") when the
   reply states a lasting preference. Never turn one contact's behaviour into
   a playbook rule.
