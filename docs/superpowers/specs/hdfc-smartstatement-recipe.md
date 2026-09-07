# HDFC SmartStatement — automated retrieval (verified 2026-09-07)

The statement email carries **no attachment**. It links to a JSP that hides the
statement behind a password form, a server token and two encryption layers.
All of it is reproducible without a browser. Verified end to end against a real
statement (a/c ***1225, period 12/07/2026–11/08/2026).

## The flow

1. **Extract the link** from the email HTML: `href` containing `GetStatement.jsp`.
2. **GET that URL** with a cookie jar. Scrape three values from the page:
   - `ke`      — hidden input, the jobkey
   - `seqence` — hidden input, session-derived (changes on every GET)
   - the form `action`, currently `./webresources/app/htmlformat`
3. **GET `<base>/CRSGetToken?jobkey=<ke>`** on the same session. The body is a
   ~42-char token. Skipping this step is why a direct POST returns
   `HTTP 417 Internal Error occured` — it is not a password rejection.
4. **POST** the form action with `ke`, `seqence`, and
   `pwd = encrypt(token + password)`.
5. The response is HTML containing `Data("<key b64>","<ciphertext b64>")`.
   Decrypt: **AES-128-ECB, PKCS7**, key = base64-decode of the first argument
   (observed: `MTIzNF5eXl5eXl5eXl5eXg==` → `1234^^^^^^^^^^^^`). Read the key from
   the page, never hardcode it.
6. The plaintext is the statement page. The transactions are a real HTML table,
   `id="Table76"`, columns:
   `Date | Transaction details | Cheque/Ref No | Value date | Withdrawal | Deposit | Closing Balance`

## `encrypt()` — from the page's obfuscated `js/encrypt.js`

A chained XOR stream. The key is the literal string `toUpperCase`.

```python
KEY = "toUpperCase"
def encrypt(s: str) -> str:
    seed = int((random.random() * 10000) % 255) + 1
    out, ki = f"{seed:02x}", -1
    for ch in s:
        t = (ord(ch) + seed) % 255
        ki = ki + 1 if ki < len(KEY) - 1 else 0
        t ^= ord(KEY[ki])
        out += f"{t:02x}"
        seed = t
    return out.upper()
```

## Notes

- The password is the ordinary HDFC statement password (first 4 letters of the
  name in upper case + DDMM of birth, or + first 4 digits of the customer ID).
  Derive it from stored components; never store the string.
- The column name is `Transaction details`, NOT `Narration` — that is the PDF's
  wording. Matching on the wrong one silently finds no rows.
- Validate every parsed statement by its own arithmetic: closing − opening must
  equal deposits − withdrawals. Refuse the statement if it does not.
- Links on the retired `smartstatements.hdfcbank.com` domain no longer resolve;
  only `smartstatements.hdfc.bank.in` is live.

## Links expire — fetch promptly

A statement job is purged server-side after roughly three months. The JSP still
renders its password form, and the token step still succeeds, but the POST
returns `input XML file not existed` (27 bytes, HTTP 200). Verified 2026-09-07:
the August and September 2026 statements retrieved cleanly; the June 2026 ones
for the same two accounts returned that message.

Two consequences:

- The ingest must run **on arrival**, driven by the statement email, not as a
  periodic sweep over an old mailbox. A backfill can only recover the last few
  months.
- Distinguish the three failure bodies. `Internal Error occured` means the
  `CRSGetToken` step was skipped or the session was lost; `input XML file not
  existed` means the job is gone and retrying will never help; a short body
  after a good token means the password was wrong. Only the last is worth
  trying another password for.
