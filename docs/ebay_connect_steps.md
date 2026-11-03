# Connect eBay so Gear Scout can send Best Offers

You do this once. It lets the "Send offer on eBay" button work on Buy It Now listings that take Best Offer. Gear Scout always asks you to confirm before it sends anything, and an offer is binding.

Before you start: your eBay account needs a payment method on file, or eBay will refuse offers. Your eBay App ID and Cert ID must already be saved in Settings > eBay (the same keys used for eBay search).

## Steps

1. Sign in at https://developer.ebay.com/ with your eBay account.
2. Go to **Application Keys** (under your account menu).
3. Find the **Production** keyset (not Sandbox).
4. In that row, under **User Tokens**, click **Get a Token from eBay via Your Application**, then **Add eBay Redirect URL** (the wording varies slightly).
5. Fill in:
   - **Display Title:** Gear Scout
   - **Your auth accepted URL:** your dashboard's public https address followed by `/ebay/callback`. Use your ngrok address, for example `https://your-name.ngrok-free.app/ebay/callback`.
   - **Your auth declined URL:** the same address followed by `/settings`, for example `https://your-name.ngrok-free.app/settings`.
6. Save. eBay shows a **RuName**, a string that looks like `Name-AppName-PRD-xxxxxxxx-xxxxxxxx`. Copy it.
7. In Gear Scout open **Settings > eBay**, paste the RuName into the RuName box under "Send offers from your eBay account" and press **Save RuName**. (Alternatively put it in config.yaml under `ebay_api:` as `runame:`.)
8. Press **Connect eBay**, sign in to eBay, and click **Agree**. You land back on Settings with a green "eBay account connected" message.

If your ngrok address ever changes, repeat steps 4 to 6 with the new address and save the new RuName.

## Notes

- The Send offer on eBay button appears only on eBay Buy It Now cards that take Best Offer and have a suggested offer. Listings seen before this feature may not show it until they are scraped again.
- To remove access, press **Disconnect** in Settings > eBay. You can also revoke it in your eBay account's permissions page.
- Gear Scout stores only a refresh token in config.yaml (`ebay_api.user_refresh_token`). The first time it writes there, it saves a copy as `config.yaml.bak-ebay` next to config.yaml if it can.
