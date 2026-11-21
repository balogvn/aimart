# Selling the converter on Apify Store

The `apify/` folder of this repository is an **Apify Actor**: the converter packaged so that people
and agents with an Apify account (or a prepaid Apify token bought with x402, MPP, a card or
Skyfire) can run it from [Apify Store](https://apify.com/store) and pay per use. Apify bills
them and pays you monthly. It does not replace the x402 service on Modal; it is a second shop
window with a different set of buyers.

Everything Apify-specific below was checked against Apify's documentation and the Apify Python
SDK's source on 2026-11-17. Apify changes its Console often: if a button has moved, the page
named in each step still exists under **Development > My Actors > (the Actor)**.

## How it works and what it charges

- Each input (a public link, or a small file sent as base64) is converted by the same code as
  the hosted service: package `aimart-parquet` 0.1.2 from PyPI, with the same limits (50 MB per
  file, 120 s per conversion, the 700 MB job memory cap) and the same protection against links
  to private or local network addresses. Inputs that name files on the machine running the
  Actor are refused.
- The Parquet file goes into the run's key-value store, with a download link; one dataset item
  per input reports the result, or the error with its code and hint.
- Two pay-per-event events, charged by the Actor's code (`apify/aimart_actor/core.py`):

  | event name (type it exactly) | price | charged when |
  |---|---|---|
  | `file-converted` | $0.02 | a file was converted and its Parquet stored |
  | `query-run` | $0.005 | the optional SQL ran successfully over one converted file (even if it matched no rows) |

  These are the hosted service's prices. A failed input is never charged. Before each event the
  Actor checks that the run's maximum charge (set by the user) still covers it; if not, it stops
  and reports the remaining inputs as `skipped`, uncharged.
- Apify keeps 20% of what users pay. The platform's cost of running the Actor (memory and time)
  is taken from your share (Apify's formula: profit = 0.8 x revenue - platform costs): a run of
  a few small files takes a few seconds at 2 GB of memory, about $0.001 to $0.002 at Apify's
  $0.20 per compute unit (1 GB for one hour), so a $0.02 conversion leaves you about $0.014.
  Users on Apify's free plan pay nothing and earn you nothing (Apify covers their usage).
- **Failed inputs cost you, not the user.** A failure is free for the user, but the time the
  Actor spent on it is platform cost, taken from your share. Most failures take under a second
  (a broken link, an unreadable file). The expensive one is a file that runs into the 120-second
  conversion limit. So the Actor keeps a **failure budget**: once failed inputs have used
  more than 120 seconds of a run in total, it does not start the remaining inputs. It reports them
  as `skipped` with the code `failure_budget_reached`, and nothing is charged for them. The most
  a run can then spend on failures is just under 4 minutes at 2 GB, about 0.13 compute units,
  or about **$0.03 per run**, paid by you. Someone who sends only bad files, run after run,
  costs you about that much per run, and Apify bills them nothing. If that happens, Apify's
  Insights pages show many runs with no revenue. The only full protection is charging for the
  platform's usage as well ("Pay per event + usage" in the monetization settings), but then a
  failure is no longer free for the user, so this guide leaves it off.
- **Restarted runs are not charged twice.** Apify sometimes moves a running Actor to another
  server, and a user can resurrect a finished run. Either way the Actor starts again from its
  first input, with the same storage. The Actor records each input's progress in the run's
  key-value store (records named `progress-001`, `progress-002` and so on), so a restarted run
  skips what it already reported and charges nothing again. In the rare case that the server
  stops during a charge, the Actor treats that charge as made. It never charges twice; at worst
  one file is delivered without payment.

## Steps only you can do

You need your Apify account (you have one) and about 30 minutes. Setting up costs nothing; the
test runs in steps 3 and "How to check that it works" each use a few seconds of platform time
on your account.

### 1. Create the Actor from the GitHub repository

1. Sign in at <https://console.apify.com>.
2. In the left panel, click **Development**, then **My Actors**.
3. Click **Develop new**, then **Connect Git**.
4. At **Select Git provider**, click **GitHub**. GitHub opens and asks you to authorize the
   Apify app: you may choose **Only select repositories** and pick **balogvn/aimart**. Confirm.
   (This lets Apify rebuild the Actor on every push to the repository.)
5. Back in Apify, at **Pick a repo from GitHub**, click **balogvn/aimart**. Apify creates the
   Actor and opens it.
6. Open the Actor's **Source** tab. The **Git URL** field shows the repository. Change it to
   exactly:

   ```text
   https://github.com/balogvn/aimart#main:apify
   ```

   (`#main` is the branch, `:apify` the folder that holds the Actor.) Click **Save**.

If you would rather not install the Apify app on GitHub: in step 3 create an empty Actor
instead, then in its **Source** tab set **Source type** to **Git repository** and paste the same
Git URL. The repository is public, so no key is needed; you then click **Build** yourself after
each change.

### 2. Build it

1. In the Actor, click **Build** (top right, or in the **Source** tab).
2. Wait for the build to finish (a few minutes the first time). The build log ends with a line
   like `aimart-parquet 0.1.2 apify 4.2.0`. A red build means something is wrong: copy the
   last 30 lines of the log and send them to whoever maintains the repository.

### 3. Test-run it with the sample

1. Open the Actor's **Input** tab. **File links** is already filled with the sample:
   `https://raw.githubusercontent.com/balogvn/aimart/main/docs/examples/sales.csv`.
2. In **SQL query**, paste:
   `SELECT region, sum(units * unit_price) AS revenue FROM data GROUP BY region ORDER BY revenue DESC`
3. Click **Start**. The run should finish as **Succeeded** within a minute.
4. In the run's **Output** tab you should see one row: status `converted`, 6 rows, a
   **Parquet file** link, and in the **Query results** view four regions with North first
   (551.88). Click the Parquet link: a file named `parquet-001-sales.parquet` downloads.
5. Before monetization the run is free, so the item's `charged` list is empty. That is expected.

### 4. Set pay-per-event pricing

First add your billing details, which Apify asks for before monetization (step 6 covers payouts).

1. Open the Actor's **Publication** tab (called **Publishing** in some of Apify's pages), find
   **Monetization**, and click **Set up monetization**. Choose **Pay per event**.
2. In the **Actor pricing** step:
   - Click **Add event**. **Event name**: `file-converted`. Title: `File converted`.
     Description: `One input file converted to Parquet and stored.` Price: `0.02`.
   - Click **Add event** again. **Event name**: `query-run`. Title: `Query run`.
     Description: `One SQL query run over one converted file.` Price: `0.005`.
   - Give each event **one price for every discount tier** (no separate FREE, BRONZE, SILVER,
     GOLD prices). The Apify SDK version this Actor uses (4.2.0) does not charge an event
     priced per tier, so per-tier prices would give the work away; the Actor refuses to run
     rather than do that.
   - **Delete** the event `apify-default-dataset-item` (Apify adds it by default). It charges
     for every dataset item, including the items that report a failed input. **This is
     required**: while it has a price, the Actor refuses every run, with a message saying so.
   - **Delete** the event `apify-actor-start` too. **This is required**: it charges $0.00005
     for each GB of memory (so $0.0001 at the Actor's 2 GB) every time a run starts, even when
     every input fails. The Store page promises that failures cost nothing and that there is no
     fee beyond the two events above. The cost to you: Apify no longer covers the first 5
     seconds of each run, so that compute (about $0.0006 a run at 2 GB) comes out of your
     share. The Actor cannot check this one for you, because Apify charges it before the Actor's
     code starts.
   - Leave **Pay per event + usage** (under additional options) **off**: the prices above
     include the platform's cost.
   - If asked for a minimal **max cost per run**, enter `0.03` (one file and its query).
3. **Primary event**: choose `file-converted`.
4. **Review**: check that exactly two events are listed, `file-converted` at $0.02 and
   `query-run` at $0.005 (no `apify-actor-start`, no `apify-default-dataset-item`), and
   confirm.

Spelling matters: an event name the code does not use is never charged, and if either name is
missing, every run stops at once with "This Actor's pricing has no flat-priced event named ...".

### 5. Check the permissions

1. Open the Actor's **Settings** tab.
2. Under **Actor permissions**, the dropdown must say **Limited permissions** (the default for
   new Actors). If it says Full, change it to Limited and save.

Limited permissions are what lets agents with a prepaid token run the Actor without an Apify
account, and they raise the Actor's quality score in the Store. The Actor only uses its own
run's storages, so it needs nothing more.

### 6. Payout details

1. In the left panel, click **Development**, then **Insights**, then the **Payouts** tab.
2. Fill in **Billing details** and choose a payout method. PayPal or Wise pay out from $20 a
   month; other methods from $100. Unpaid amounts roll over to the next month.
3. Click **Verify identity** and follow the prompts (a photo of your ID card or driving licence,
   your full legal name). Without it, earnings roll over and are not paid.

Apify writes the invoice on the 11th of each month for the previous month and pays between the
21st and 25th.

### 7. Publish to Apify Store

1. Open the Actor's **Publication** tab.
2. **Display information**: upload a square logo (any simple image; there is none in the
   repository yet), keep the title `AImart Parquet: messy files to Parquet + SQL`, and pick the
   categories that fit best from the list Apify shows (for example a developer tools, data or
   AI category). The description is filled from the Actor; the long page is
   `apify/README.md` from the repository.
3. **Monetization**: shows the two events from step 4.
4. **Sample output**: run the sample (step 3) once more if Apify asks for one.
5. **Output schema**: already provided by the repository (`apify/.actor/output_schema.json`).
6. **Actor permissions**: Limited.
7. Accept the **Store Publishing Terms and Conditions** if asked, then click **Publish on
   Store**.

## How to check that it works

1. **The Store page**: open `https://apify.com/<your Apify username>/aimart-parquet` in a
   private browser window. It should show the README, the two prices and a **Limited
   permissions** badge.
2. **A paid run**: run the sample again (step 3) after step 4. In the run's **Output**, the
   item's **Charged** column should say `file-converted, query-run`. The run's detail page lists
   the charged events and their total ($0.025).
3. **A failure costs nothing**: run it with **File links** set to
   `https://raw.githubusercontent.com/balogvn/aimart/main/docs/examples/no-such-file.csv`. The
   item says `failed`, `fetch_failed`, and **Charged** is empty; the run's charged total is $0.
4. **The limit**: run the sample with two links (the sample twice) and **Max cost per run** set
   to `0.03` in the run options. The first file is converted and queried, the second is
   `skipped` with `charge_limit_reached`, and the run charges $0.025.
5. **Apify's own daily test** runs the Actor with the prefilled input (the sample) and expects a
   successful run with a non-empty dataset within 5 minutes; a failing Actor is marked **Under
   maintenance** after three days. If you get that email, look at the run it links to.
6. **For agents**: in an MCP client connected to `https://mcp.apify.com`, the tool
   `fetch-actor-details` with your Actor's name shows the input fields and pricing an agent sees.

## Updating the Actor (for maintainers)

- The Actor is `apify/`: `.actor/` (definition and schemas Apify reads), `Dockerfile`,
  `requirements.txt` (every package pinned with hashes), `aimart_actor/core.py` (the work,
  tested in `tests/unit/test_apify_actor.py` without the Apify SDK) and `aimart_actor/actor.py`
  (the SDK binding), and `README.md` (the Store page).
- To move to a new `aimart-parquet` release, change its pin and regenerate `requirements.txt`
  from the repository's root at that release's commit (`$UV` is your uv). The dependencies of
  `aimart-parquet` stay at its `uv.lock`; the packages only the Apify SDK brings are held to
  releases at least 14 days old (change the date):

  ```bash
  $UV export --frozen --no-dev --no-hashes --no-emit-project --no-header --no-annotate > /tmp/lock.txt
  printf 'aimart-parquet==0.1.2\napify==4.2.0\n' > /tmp/req.in
  pins=(); for p in aimart-parquet $(sed 's/[= ;].*//' /tmp/lock.txt); do pins+=(--exclude-newer-package "$p=2100-01-01T00:00:00Z"); done
  $UV pip compile /tmp/req.in -c /tmp/lock.txt -p 3.13 --python-platform x86_64-manylinux_2_28 \
    --exclude-newer 2026-11-03T00:00:00Z "${pins[@]}" --generate-hashes --no-header --no-annotate
  ```

  Paste the output under the comment at the top of `apify/requirements.txt`, update the dates
  in that comment, and run `make check` (a test reads the pins).
- Known limitations (each errs on the side of the user, never charging twice or for a
  failure):
  - A run restarted while a charge was in flight counts that charge as made (its outcome
    cannot be read back). At most one input per restart may be delivered unpaid.
  - If the restart came right after a file's conversion was charged and before its query ran,
    that query is reported as `query_interrupted`, unpaid; it is not run again.
  - A charge refused at the limit withdraws the stored Parquet and then records the result; a
    restart in the instant between the two would report that file as converted and charged
    although nothing was charged and its link no longer works.
  - Failed inputs cost the owner up to about $0.03 of compute per run (the failure budget,
    `FAILURE_BUDGET_S` in `core.py`); runs made only of failing inputs are paid by the owner.
- To try the Actor locally: install the pinned packages into a virtual environment, install
  `apify-cli` (`npm install apify-cli@1.11.0` in a scratch folder), and from `apify/` run
  `apify run --purge --entrypoint aimart_actor --input-file input.json` with that environment's
  `python` first on `PATH`. Add `ACTOR_TEST_PAY_PER_EVENT=true` (and optionally
  `ACTOR_MAX_TOTAL_CHARGE_USD=2.5`) to log charges to `storage/datasets/charging-log/` instead
  of billing; locally every event costs $1, Apify's per-item event included. The image builds
  with `docker build -t aimart-actor apify/` and runs with the input in
  `storage/key_value_stores/default/INPUT.json` mounted at `/usr/src/app/storage`.
