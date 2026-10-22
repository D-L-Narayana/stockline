import { MoneyPipe } from './money.pipe';

describe('MoneyPipe', () => {
  const pipe = new MoneyPipe();

  it('formats paise as rupees with two decimals', () => {
    expect(pipe.transform(64900)).toBe('₹649.00');
    expect(pipe.transform(5)).toBe('₹0.05');
  });

  it('uses Indian digit grouping for large values', () => {
    expect(pipe.transform(199900 * 100)).toBe('₹1,99,900.00');
  });

  it('handles negatives and empty values', () => {
    expect(pipe.transform(-150)).toBe('-₹1.50');
    expect(pipe.transform(null)).toBe('—');
    expect(pipe.transform(undefined)).toBe('—');
  });
});
