// Pay AImart Parquet from a Circle developer-controlled wallet: convert a CSV, then query it.
//
//   export CIRCLE_API_KEY=... CIRCLE_ENTITY_SECRET=...
//   export AIMART_MAINNET_CONFIRM=I_UNDERSTAND_THIS_SPENDS_REAL_USDC
//   npx tsx circle.ts --wallet 0xYourCircleWalletOnBase
//
// It spends $0.025 of USDC on Base: $0.02 to convert, $0.005 to query. The wallet must be an EOA
// on BASE holding USDC, and nothing else: no ETH, no approval. Recipe: docs/recipes/circle-typescript.md.
//
// `circleSigner` is the only Circle code. Everything else is in aimart.ts and takes any signer.
import { initiateDeveloperControlledWalletsClient } from "@circle-fin/developer-controlled-wallets";
import type { ClientEvmSigner } from "@x402/evm";
import { isMain, makePayer, type Network, runFromCommandLine } from "./aimart.js";

const CIRCLE_CHAINS = { "eip155:8453": "BASE", "eip155:84532": "BASE-SEPOLIA" } as const;
const EIP712_DOMAIN = [
  { name: "name", type: "string" },
  { name: "version", type: "string" },
  { name: "chainId", type: "uint256" },
  { name: "verifyingContract", type: "address" },
];

/** Your Circle developer-controlled wallet (its 0x address) as an x402 signer. */
export function circleSigner(wallet: string, network: Network): ClientEvmSigner {
  const apiKey = process.env.CIRCLE_API_KEY;
  const entitySecret = process.env.CIRCLE_ENTITY_SECRET;
  if (!apiKey || !entitySecret) throw new Error("set CIRCLE_API_KEY and CIRCLE_ENTITY_SECRET");
  const circle = initiateDeveloperControlledWalletsClient({ apiKey, entitySecret });
  return jsonSigner(wallet, async (data) => {
    // One call to Circle's sign-typed-data API; the SDK encrypts a fresh entity-secret ciphertext.
    const response = await circle.signTypedData({
      walletAddress: wallet,
      blockchain: CIRCLE_CHAINS[network],
      data,
      memo: "AImart Parquet x402 payment",
    });
    const signature = response.data?.signature;
    if (!signature) throw new Error("Circle returned no signature");
    return signature;
  });
}

/**
 * x402's signer over any function that signs one EIP-712 JSON document, as Circle's API does.
 * x402 hands over the typed data without EIP712Domain and with bigints, so the domain type is
 * added and numbers become decimal strings, as Circle's own x402 quickstart sends them.
 */
export function jsonSigner(wallet: string, signJson: (data: string) => Promise<string>): ClientEvmSigner {
  return {
    address: wallet as `0x${string}`,
    async signTypedData({ domain, types, primaryType, message }) {
      const data = JSON.stringify(
        { types: { EIP712Domain: EIP712_DOMAIN, ...types }, primaryType, domain, message },
        (_key, value) => (typeof value === "bigint" ? value.toString() : value),
      );
      const signature = await signJson(data);
      return (signature.startsWith("0x") ? signature : `0x${signature}`) as `0x${string}`;
    },
  };
}

if (isMain(import.meta.url)) {
  await runFromCommandLine("wallet", async (wallet, network, maxUsd) => {
    console.log(`payer: ${wallet}`);
    return makePayer(circleSigner(wallet, network), network, maxUsd);
  });
}
