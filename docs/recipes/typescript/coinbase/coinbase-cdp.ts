// Pay AImart Parquet from a Coinbase CDP server wallet: convert a CSV, then query it.
//
//   export CDP_API_KEY_ID=... CDP_API_KEY_SECRET=... CDP_WALLET_SECRET=...
//   export AIMART_MAINNET_CONFIRM=I_UNDERSTAND_THIS_SPENDS_REAL_USDC
//   npx tsx coinbase-cdp.ts --account 0xYourCdpAccount
//
// It spends $0.025 of USDC on Base: $0.02 to convert, $0.005 to query. The account needs USDC on
// Base and nothing else: no ETH, no approval. Recipe: docs/recipes/coinbase-cdp-typescript.md.
//
// `coinbaseSigner` is the only Coinbase code. Everything else is in aimart.ts and takes any signer.
import { CdpClient } from "@coinbase/cdp-sdk";
import { fromCdpEvmAccount } from "@coinbase/cdp-sdk/x402";
import type { ClientEvmSigner } from "@x402/evm";
import { isMain, makePayer, runFromCommandLine } from "./aimart.js";

/** Your CDP EVM account (a 0x address or a name) as an x402 signer. The key stays at Coinbase. */
export async function coinbaseSigner(account: string): Promise<ClientEvmSigner> {
  // CdpClient reads CDP_API_KEY_ID, CDP_API_KEY_SECRET and CDP_WALLET_SECRET from the environment.
  const cdp = new CdpClient();
  const server = account.startsWith("0x")
    ? await cdp.evm.getAccount({ address: account as `0x${string}` })
    : await cdp.evm.getAccount({ name: account });
  console.log(`payer: ${server.address}`);
  return fromCdpEvmAccount(server); // each payment is one call to CDP's sign-typed-data API
}

if (isMain(import.meta.url)) {
  await runFromCommandLine("account", async (account, network, maxUsd) =>
    makePayer(await coinbaseSigner(account), network, maxUsd),
  );
}
