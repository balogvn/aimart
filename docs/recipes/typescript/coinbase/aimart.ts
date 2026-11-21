// The payment part of the recipes: it takes any x402 signer, whoever holds the key.
// The same file sits in typescript/coinbase and typescript/circle; a test keeps them equal.
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { x402Client } from "@x402/core/client";
import type { ClientEvmSigner } from "@x402/evm";
import { ExactEvmScheme } from "@x402/evm/exact/client";
import { wrapFetchWithPayment } from "@x402/fetch";

export const AIMART_URL = "https://balogvn--aimart-parquet.us-east.modal.run";
export const SAMPLE_CSV = "https://raw.githubusercontent.com/balogvn/aimart/main/docs/examples/sales.csv";
export const SQL = "SELECT region, sum(units) AS units FROM data GROUP BY region ORDER BY region";
export const NETWORKS = { base: "eip155:8453", "base-sepolia": "eip155:84532" } as const;
export type Network = (typeof NETWORKS)[keyof typeof NETWORKS];
const EXPLORERS: Record<string, string> = {
  "eip155:8453": "https://basescan.org",
  "eip155:84532": "https://sepolia.basescan.org",
};
const MAINNET_CONFIRM = "I_UNDERSTAND_THIS_SPENDS_REAL_USDC";
const USDC_DECIMALS = 6; // 20000 atomic units are $0.02

/** A call that did not deliver. Its message is one line that says whether money moved. */
export class NotPaid extends Error {}

/** USD as USDC atomic units, exactly: "0.02" is 20000n. */
export function atomic(usd: string): bigint {
  const [whole, fraction = ""] = usd.trim().replace(/^\$/, "").split(".");
  if (!/^\d+$/.test(whole) || !/^\d{0,6}$/.test(fraction)) throw new Error(`not a USD amount: ${usd}`);
  return BigInt(whole) * 10n ** BigInt(USDC_DECIMALS) + BigInt(fraction.padEnd(USDC_DECIMALS, "0"));
}

/** An x402 client that signs only on `network` and never above `maxUsd` per call. */
export function makePayer(signer: ClientEvmSigner, network: Network, maxUsd = "0.02"): x402Client {
  const cap = atomic(maxUsd);
  return new x402Client()
    .register(network, new ExactEvmScheme(signer))
    .registerPolicy((_version, options) => options.filter((o) => BigInt(o.amount) <= cap)); // a dearer quote is never signed
}

/** A base64 JSON payment header as an object; {} when it is absent or unreadable. */
export function decode(header: string | null): Record<string, any> {
  try {
    const value = JSON.parse(Buffer.from(header ?? "", "base64").toString("utf8"));
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  } catch {
    return {};
  }
}

export function readHeader(response: Response, name: string): Record<string, any> {
  return decode(response.headers.get(name));
}

/** The fetch under x402's: it notes each signed payment it hands to the network, so a call
 * that gets no answer can say whether money may have moved. */
export function watchPayments(fetchImpl: typeof fetch) {
  const sent: Record<string, any>[] = []; // each PAYMENT-SIGNATURE sent, decoded
  const watched = (input: RequestInfo | URL, init?: RequestInit) => {
    const headers = input instanceof Request ? input.headers : new Headers(init?.headers);
    const signed = headers.get("PAYMENT-SIGNATURE") ?? headers.get("X-PAYMENT");
    if (signed) sent.push(decode(signed));
    return fetchImpl(input, init);
  };
  return { fetch: watched as typeof fetch, sent };
}

/** A signed payment went out and no answer came back: it may have been settled. */
export function outcomeUnknown(payment: Record<string, any>, reason: string): string {
  const accepted = payment.accepted ?? {};
  const payer = String(payment.payload?.authorization?.from ?? "");
  const explorer = EXPLORERS[accepted.network] ?? "https://basescan.org";
  const where = payer.startsWith("0x") ? `${explorer}/address/${payer}` : `your wallet on ${explorer}`;
  return (
    `outcome unknown, a signed payment of ${usdc(BigInt(accepted.amount ?? 0))} USDC was sent and may ` +
    `have been settled (${reason}): look up ${where} before paying again`
  );
}

/** Why a paid call did not deliver. A 402's body is {}: the reason is in a header. */
export function failure(response: Response, text: string): string {
  if (response.status !== 402) {
    let error: Record<string, any> = { message: text.slice(0, 200) };
    try {
      const parsed = JSON.parse(text)?.error; // {"error": {"code", "message", "hint"}}
      if (parsed && typeof parsed === "object") error = parsed;
    } catch {}
    const said = `${error.message ?? ""} ${error.hint ?? ""}`.split(/\s+/).join(" ").trim();
    return `HTTP ${response.status}, not charged: ${said}`;
  }
  const rejected = readHeader(response, "PAYMENT-REQUIRED");
  const receipt = readHeader(response, "PAYMENT-RESPONSE");
  if (Object.keys(receipt).length === 0) {
    return `payment rejected, not charged: ${rejected.error || "no reason given"}`;
  }
  const why = receipt.errorReason || "no reason given";
  if (receipt.transaction) {
    return (
      `payment not settled, result withheld: ${why}; the facilitator named transaction ` +
      `${receipt.transaction}: look it up before paying again, the transfer may have been made`
    );
  }
  return `payment not settled, result withheld: ${why}`;
}

/** Atomic units as USDC: 20000n is "0.02". */
function usdc(amount: bigint): string {
  const unit = 10n ** BigInt(USDC_DECIMALS);
  return `${amount / unit}.${(amount % unit).toString().padStart(USDC_DECIMALS, "0")}`.replace(/\.?0+$/, "");
}

export function paidLine(receipt: Record<string, any>): string {
  const amount = usdc(BigInt(receipt.amount ?? 0));
  const transaction: string = receipt.transaction || "not named";
  const explorer = EXPLORERS[receipt.network];
  const link = explorer && transaction.startsWith("0x") ? ` ${explorer}/tx/${transaction}` : "";
  return `paid ${amount} USDC, tx ${transaction}${link}`;
}

/**
 * Convert `source` (default: the sample CSV's URL) and query it: two paid calls. Throws NotPaid
 * with one line when a call does not deliver: a call that fails is not charged, and a paid call
 * whose answer is lost says so. The artifact is printed as soon as the conversion is paid for.
 */
export async function convertAndQuery(
  payer: x402Client,
  { baseUrl = AIMART_URL, source = { url: SAMPLE_CSV } as Record<string, unknown>, sql = SQL, fetchImpl = fetch } = {},
) {
  const watch = watchPayments(fetchImpl);
  const pay = wrapFetchWithPayment(watch.fetch, payer);
  const post = async (path: string, body: unknown) => {
    const before = watch.sent.length;
    let response: Response;
    let text: string;
    try {
      response = await pay(`${baseUrl}${path}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      text = await response.text();
    } catch (error) {
      const reason = `${error instanceof Error ? error.message : error}`.split(/\s+/).join(" ");
      // Only the watch knows whether a signed payment left before the answer was lost.
      if (watch.sent.length > before) throw new NotPaid(`${path}: ${outcomeUnknown(watch.sent.at(-1)!, reason)}`);
      // The x402 client throws this before anything is signed: over the cap, or the wallet failed.
      const unpaid = reason.startsWith("Failed to create payment payload");
      throw new NotPaid(`${path}: ${unpaid ? "no payment made" : "no response, nothing paid"}: ${reason}`);
    }
    if (response.status !== 200) throw new NotPaid(`${path}: ${failure(response, text)}`);
    return { result: JSON.parse(text), receipt: readHeader(response, "PAYMENT-RESPONSE") };
  };
  const converted = await post("/v1/convert", source);
  const artifact = converted.result.artifact;
  console.log(`convert: ${converted.result.report.rows} rows; ${paidLine(converted.receipt)}`);
  console.log(`artifact: ${artifact.id} (until ${artifact.expires_at})`);
  const queried = await post("/v1/query", { artifact_id: artifact.id, sql });
  console.log(
    `query: ${JSON.stringify(queried.result.columns)} ${JSON.stringify(queried.result.rows)}; ${paidLine(queried.receipt)}`,
  );
  return { artifactId: artifact.id as string, convert: converted, query: queried };
}

/** True when `moduleUrl` (import.meta.url) is the file node or tsx was asked to run. */
export function isMain(moduleUrl: string): boolean {
  return process.argv[1] !== undefined && moduleUrl === pathToFileURL(process.argv[1]).href;
}

/**
 * The command line every recipe shares: `--<walletFlag> <value> [--max-usd 0.02]
 * [--network base|base-sepolia] [--url ...]`. `makeClient` is the wallet-specific part.
 */
export async function runFromCommandLine(
  walletFlag: string,
  makeClient: (wallet: string, network: Network, maxUsd: string) => Promise<x402Client>,
): Promise<void> {
  const usage = `usage: --${walletFlag} <value> [--max-usd 0.02] [--network base|base-sepolia] [--url ${AIMART_URL}]`;
  const stop = (line: string) => {
    console.error(line);
    process.exit(1);
  };
  let values: Record<string, string | boolean | undefined>;
  try {
    ({ values } = parseArgs({
      options: {
        [walletFlag]: { type: "string" },
        "max-usd": { type: "string", default: "0.02" },
        network: { type: "string", default: "base" },
        url: { type: "string", default: process.env.AIMART_URL ?? AIMART_URL },
        help: { type: "boolean", short: "h" },
      },
    }));
  } catch (error) {
    return stop(`${error instanceof Error ? error.message : error}; ${usage}`);
  }
  if (values.help) {
    console.log(usage);
    return;
  }
  const wallet = values[walletFlag];
  const network = NETWORKS[values.network as keyof typeof NETWORKS];
  if (typeof wallet !== "string") return stop(`pass --${walletFlag}; ${usage}`);
  if (!network) return stop(`--network is one of: ${Object.keys(NETWORKS).join(", ")}`);
  if (network === NETWORKS.base && process.env.AIMART_MAINNET_CONFIRM !== MAINNET_CONFIRM) {
    return stop(`this spends real USDC on Base: set AIMART_MAINNET_CONFIRM=${MAINNET_CONFIRM}`);
  }
  const maxUsd = String(values["max-usd"]);
  try {
    const payer = await makeClient(wallet, network, maxUsd);
    console.log(`network ${network}, at most $${maxUsd} a call`);
    await convertAndQuery(payer, { baseUrl: String(values.url).replace(/\/+$/, "") });
  } catch (error) {
    stop(error instanceof NotPaid ? error.message : `not paid: ${error instanceof Error ? error.message : error}`);
  }
}
