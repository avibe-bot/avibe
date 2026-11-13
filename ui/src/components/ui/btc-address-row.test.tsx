/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import en from '@/i18n/en.json';
import { BtcAddressRow, KeyAddressList } from './btc-address-row';

afterEach(cleanup);
const i18n = createInstance();
void i18n.use(initReactI18next).init({ lng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
const a = en.vaults.addresses;
const ADDR = {
  p2tr: 'bc1p5cyxnuxmeuwuvkwfem96lqzszd02n6xdcjrs20cac6yqjjwudpxqkedrcr',
  p2wpkh: 'bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu',
  p2pkh: '1LqBGSKuX5yYUonjxT5qGfpUsXKYYWeabA',
};
const wrap = (node: React.ReactNode) => render(<I18nextProvider i18n={i18n}>{node}</I18nextProvider>);
const chip = () => screen.getByRole('button', { name: new RegExp(`^${a.btcType}`) });

it('shows one BTC address, Taproot first, and switches among the types the server lists', async () => {
  wrap(<BtcAddressRow types={['p2pkh', 'p2wpkh', 'p2tr']} addresses={ADDR} />);
  expect(screen.getByText(ADDR.p2tr)).toBeTruthy();
  expect(screen.queryByText(ADDR.p2wpkh)).toBeNull();
  await act(async () => { fireEvent.click(chip()); });
  const options = screen.getAllByRole('radio');
  expect(options.map((o) => o.textContent)).toEqual([`${a.typeTaproot}${a.prefixTaproot}`, `${a.typeSegwit}${a.prefixSegwit}`, `${a.typeLegacy}${a.prefixLegacy}`]);
  await act(async () => { fireEvent.click(screen.getByRole('radio', { name: new RegExp(a.typeSegwit) })); });
  expect(screen.getByText(ADDR.p2wpkh)).toBeTruthy();
  expect(screen.queryByText(ADDR.p2tr)).toBeNull();
  expect(screen.queryByRole('radio')).toBeNull();
});

it('offers only the listed types, and draws no chip when there is one', () => {
  wrap(<BtcAddressRow types={['p2tr']} addresses={ADDR} />);
  expect(screen.getByText(ADDR.p2tr)).toBeTruthy();
  expect(screen.queryByRole('button', { name: new RegExp(`^${a.btcType}`) })).toBeNull();
  expect(screen.queryByText(ADDR.p2wpkh)).toBeNull();
});

it('without a type list from the server, a key shows only its Taproot address (never inferred types)', () => {
  wrap(<KeyAddressList addresses={{ eth: '0xE', btc_taproot: ADDR.p2tr, btc_segwit: ADDR.p2wpkh, btc_legacy: ADDR.p2pkh }} />);
  expect(screen.getByText(ADDR.p2tr)).toBeTruthy();
  expect(screen.queryByText(ADDR.p2wpkh)).toBeNull();
  expect(screen.queryByText(ADDR.p2pkh)).toBeNull();
  expect(screen.queryByRole('button', { name: new RegExp(`^${a.btcType}`) })).toBeNull();
});

it('a protected key the server lists as Taproot-only gets no switch even though other addresses exist', () => {
  wrap(<KeyAddressList types={['p2tr']} addresses={{ eth: '0xE', btc_taproot: ADDR.p2tr, btc_segwit: ADDR.p2wpkh, btc_legacy: ADDR.p2pkh }} />);
  expect(screen.queryByRole('button', { name: new RegExp(`^${a.btcType}`) })).toBeNull();
  expect(screen.queryByText(ADDR.p2wpkh)).toBeNull();
});
