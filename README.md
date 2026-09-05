# slokamtech

## Razorpay setup

Set these environment variables on the backend:

```env
RAZORPAY_KEY_ID=rzp_test_or_live_key_id
RAZORPAY_KEY_SECRET=your_razorpay_key_secret
RAZORPAY_WEBHOOK_SECRET=your_optional_webhook_secret
```

Use `/api/razorpay/webhook` as the Razorpay webhook URL if you enable webhooks.
