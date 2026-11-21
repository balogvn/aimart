// Pay AImart Parquet with Coinbase's ready-made x402 client, CdpX402Client: convert, then query.
//
//   export CDP_API_KEY_ID=... CDP_API_KEY_SECRET=... CDP_WALLET_SECRET=...
//   export AIMART_MAINNET_CONFIRM=I_UNDERSTAND_THIS_SPENDS_REAL_USDC
//   npx tsx coinbase-x402client.ts --account-name your-cdp-account-name
//
// CdpX402Client uses the CDP EVM account with that name, and creates it if it does not exist yet
// (a new account holds no USDC, so the first call would be refused). It spends $0.025 of USDC on
// Base. Recipe: docs/recipes/coinbase-x402client-typescript.md.
//
// `coinbaseClient` is the only Coinbase code. Everything else is in aimart.ts.
import { CdpX402Client } from "@coinbase/cdp-sdk/x402";
import type { x402Client } from "@x402/core/client";
import { atomic, isMain, NETWORKS, type Network, runFromCommandLine } from "./aimart.js";

/** A CdpX402Client on `network`, capped at `maxUsd` per payment by its own spend controls. */
export async function coinbaseClient(accountName: string, network: Network, maxUsd: string): Promise<x402Client> {
  const client = new CdpX402Client({
    walletConfig: { type: "eoa", accountName },
    environment: network === NETWORKS.base ? "production" : "development", // Base or Base Sepolia
    spendControls: {
      maxAmountPerPayment: { atomic: atomic(maxUsd) }, // a dearer quote is never signed
      allowedNetworks: [network],
    },
  });
  console.log(`payer: ${(await client.getAddresses()).evmAddress}`);
  return client;
}

if (isMain(import.meta.url)) {
  await runFromCommandLine("account-name", coinbaseClient);
}
