import type { BitcoinAddressType } from '@/context/ApiContext';

export type BitcoinAddresses = Partial<Record<BitcoinAddressType, string>>;

const ORDER: BitcoinAddressType[] = ['p2tr', 'p2wpkh', 'p2pkh'];

/** The types to offer: the server's list, in Taproot → SegWit → Legacy order, that have an address. */
export const offeredBitcoinTypes = (types: readonly BitcoinAddressType[], addresses: BitcoinAddresses) =>
  ORDER.filter((type) => types.includes(type) && Boolean(addresses[type]));
