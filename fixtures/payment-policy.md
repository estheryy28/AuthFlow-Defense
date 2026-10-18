# Payment review and release controls

## Review record

When a reviewer approves a payment request, the review event records the payment ID, reviewer, payee, amount, and currency displayed in the review console. Those recorded payment terms are the terms the reviewer approved. The event is retained as the audit record of that review.

## Request amendments

Requesters may amend an unsettled payment, including one that has been approved but not yet settled, through the payment maintenance API. An amendment updates the existing payment request and keeps its payment ID. The amendment leaves the payment's current status unchanged. An amendment does not create a review event or change the terms recorded by an earlier review event.

## Settlement

The settlement worker reads the payee, amount, and currency from the current payment request when it processes the request. The release dispatcher submits requests whose status is `approved` to the settlement worker.
