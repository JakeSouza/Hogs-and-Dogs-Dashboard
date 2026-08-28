# Weekly Parlay — Firebase setup

One-time setup, done once by whoever sets this up (doesn't need to be you
long-term — after this, everyone interacts entirely through the dashboard
page itself, never Firebase directly).

## 1. Create the Firebase project

1. Go to https://console.firebase.google.com and click **Add project**.
2. Give it any name (e.g. "league-parlay"). Google Analytics isn't needed —
   you can leave it off.
3. Wait for the project to finish provisioning.

This uses only Firestore on the free **Spark** plan — no billing account,
no credit card, no Cloud Functions. Just the database and its security
rules.

## 2. Create the Firestore database

1. In the left sidebar: **Build > Firestore Database**.
2. Click **Create database**.
3. Choose **Start in production mode** (we'll paste in our own rules next).
4. Pick any region close to your league (doesn't meaningfully matter for
   this scale).

## 3. Publish the security rules

1. In Firestore, go to the **Rules** tab.
2. Delete the placeholder rules and paste in the entire contents of
   `firestore.rules` from this repo.
3. Click **Publish**.

## 4. Register a web app and get the config

1. Back on the project's main **Overview** page, click the **</>** (web)
   icon to add a web app.
2. Give it any nickname. You do **not** need Firebase Hosting — skip that
   checkbox.
3. Firebase will show you a config object that looks like:

   ```js
   const firebaseConfig = {
     apiKey: "AIza...",
     authDomain: "league-parlay.firebaseapp.com",
     projectId: "league-parlay",
     storageBucket: "league-parlay.appspot.com",
     messagingSenderId: "123456789",
     appId: "1:123456789:web:abcdef123456"
   };
   ```

   Copy that whole object.

## 5. Wire it into the dashboard

Set the `FIREBASE_CONFIG` environment variable to that object as a single
JSON string (same keys, just valid JSON — quote the key names too):

```
FIREBASE_CONFIG={"apiKey":"AIza...","authDomain":"league-parlay.firebaseapp.com","projectId":"league-parlay","storageBucket":"league-parlay.appspot.com","messagingSenderId":"123456789","appId":"1:123456789:web:abcdef123456"}
```

That's it — no code changes needed after this, ever, to keep submitting
and grading weekly picks. Everything from here on happens on the
dashboard page itself.

## Is it safe to put this config in a public GitHub repo / public webpage?

Yes. Unlike a typical API key, Firebase's web config is designed to be
public — it identifies *which* project a request is for, it doesn't grant
access on its own. The actual security boundary is `firestore.rules`
(step 3 above), which is what actually decides who can read or write
what. This is standard, documented Firebase behavior, not a shortcut
being taken here.

## Checking or fixing data directly (rarely needed)

If you ever need to look at the raw data yourself: Firebase console >
Firestore Database > Data tab > `parlayLegs` collection. Every leg is one
document, named `<season>-<week>-<manager-slug>` (e.g. `2026-3-jake`),
with `season`, `week`, `manager`, `pick`, and `result` fields you can edit
directly if something needs correcting. This should be a rare fallback —
the dashboard's own Grade Legs view is the normal way to fix a result.