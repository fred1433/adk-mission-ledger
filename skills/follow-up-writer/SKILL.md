---
name: follow-up-writer
description: Write a short first follow-up to a contact who asked about a service, ending with one concrete question.
metadata:
  purposes: first_contact confirm_after_reply
  channels: email
---
# Follow-up writer

Write to one contact about one request.

1. Greet by first name. One sentence that names their request.
2. Offer at most two concrete options taken from the mission facts.
3. End with one question they can answer in a word.
4. Never state a price, a discount, a waiver or an availability that is not
   in the mission facts. If the contact needs one, say a coordinator will
   confirm it.
5. Use purpose `first_contact` for the opening message and
   `confirm_after_reply` for the confirmation after they answer.
6. After `first_contact`, call `wait_for_reply` with the action id.
