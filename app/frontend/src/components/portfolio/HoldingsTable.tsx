interface PortfolioHolding {
  ticker: string;
  shares: number;
  averagePrice: number;
  currentPrice: number;
  totalValue: number;
  unrealizedPnL: number;
  unrealizedPnLPercent: number;
  weight: number;
}

interface HoldingsTableProps {
  holdings: PortfolioHolding[];
}

export function HoldingsTable({ holdings }: HoldingsTableProps) {
  const formatCurrency = (amount: number) => {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: 'USD',
    }).format(amount);
  };

  const formatPercentage = (percent: number) => {
    return `${percent >= 0 ? '+' : ''}${percent.toFixed(2)}%`;
  };

  if (holdings.length === 0) {
    return (
      <div className="text-center py-8 text-gray-500">
        No holdings in portfolio
      </div>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse">
        <thead>
          <tr className="border-b border-gray-200">
            <th className="text-left py-3 px-4 font-semibold text-gray-700">Symbol</th>
            <th className="text-right py-3 px-4 font-semibold text-gray-700">Shares</th>
            <th className="text-right py-3 px-4 font-semibold text-gray-700">Avg Price</th>
            <th className="text-right py-3 px-4 font-semibold text-gray-700">Current Price</th>
            <th className="text-right py-3 px-4 font-semibold text-gray-700">Market Value</th>
            <th className="text-right py-3 px-4 font-semibold text-gray-700">Unrealized P&L</th>
            <th className="text-right py-3 px-4 font-semibold text-gray-700">Weight</th>
          </tr>
        </thead>
        <tbody>
          {holdings.map((holding) => (
            <tr 
              key={holding.ticker} 
              className="border-b border-gray-100 hover:bg-gray-50 transition-colors"
            >
              <td className="py-3 px-4">
                <div className="flex items-center">
                  <span className="font-semibold text-blue-600">{holding.ticker}</span>
                </div>
              </td>
              <td className="text-right py-3 px-4 text-gray-700">
                {holding.shares.toLocaleString()}
              </td>
              <td className="text-right py-3 px-4 text-gray-700">
                {formatCurrency(holding.averagePrice)}
              </td>
              <td className="text-right py-3 px-4">
                <span className="font-medium">
                  {formatCurrency(holding.currentPrice)}
                </span>
              </td>
              <td className="text-right py-3 px-4 font-semibold">
                {formatCurrency(holding.totalValue)}
              </td>
              <td className="text-right py-3 px-4">
                <div className="flex flex-col items-end">
                  <span className={`font-semibold ${
                    holding.unrealizedPnL >= 0 ? 'text-green-600' : 'text-red-600'
                  }`}>
                    {formatCurrency(holding.unrealizedPnL)}
                  </span>
                  <span className={`text-sm ${
                    holding.unrealizedPnLPercent >= 0 ? 'text-green-600' : 'text-red-600'
                  }`}>
                    {formatPercentage(holding.unrealizedPnLPercent)}
                  </span>
                </div>
              </td>
              <td className="text-right py-3 px-4">
                <div className="flex items-center justify-end gap-2">
                  <span className="text-gray-700">{holding.weight.toFixed(1)}%</span>
                  <div className="w-16 bg-gray-200 rounded-full h-2">
                    <div 
                      className="bg-blue-600 h-2 rounded-full transition-all"
                      style={{ width: `${Math.min(holding.weight * 4, 100)}%` }}
                    ></div>
                  </div>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
} 