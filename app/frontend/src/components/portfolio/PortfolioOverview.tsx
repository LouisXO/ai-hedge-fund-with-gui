import { Card, CardContent } from '../ui/card';

interface PortfolioData {
  totalValue: number;
  cashBalance: number;
  totalInvested: number;
  totalPnL: number;
  totalPnLPercent: number;
  holdings: any[];
  lastUpdated: string;
}

interface PortfolioOverviewProps {
  data: PortfolioData;
  isLoading: boolean;
}

export function PortfolioOverview({ data, isLoading }: PortfolioOverviewProps) {
  const formatCurrency = (amount: number) => {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: 'USD',
    }).format(amount);
  };

  const formatPercentage = (percent: number) => {
    return `${percent >= 0 ? '+' : ''}${percent.toFixed(2)}%`;
  };

  const metrics = [
    {
      label: 'Total Portfolio Value',
      value: formatCurrency(data.totalValue),
      subValue: null,
      colorClass: 'text-blue-600',
      bgClass: 'bg-blue-50',
    },
    {
      label: 'Total P&L',
      value: formatCurrency(data.totalPnL),
      subValue: formatPercentage(data.totalPnLPercent),
      colorClass: data.totalPnL >= 0 ? 'text-green-600' : 'text-red-600',
      bgClass: data.totalPnL >= 0 ? 'bg-green-50' : 'bg-red-50',
    },
    {
      label: 'Cash Balance',
      value: formatCurrency(data.cashBalance),
      subValue: `${((data.cashBalance / data.totalValue) * 100).toFixed(1)}% of portfolio`,
      colorClass: 'text-gray-600',
      bgClass: 'bg-gray-50',
    },
    {
      label: 'Total Invested',
      value: formatCurrency(data.totalInvested),
      subValue: `${data.holdings.length} positions`,
      colorClass: 'text-purple-600',
      bgClass: 'bg-purple-50',
    },
  ];

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
      {metrics.map((metric, index) => (
        <Card key={index} className={`${metric.bgClass} border-l-4 ${metric.colorClass.replace('text-', 'border-')}`}>
          <CardContent className="p-6">
            <div className="flex items-center justify-between">
              <div className="flex-1">
                <p className="text-sm font-medium text-gray-600 mb-1">
                  {metric.label}
                </p>
                <p className={`text-2xl font-bold ${metric.colorClass} ${isLoading ? 'animate-pulse' : ''}`}>
                  {metric.value}
                </p>
                {metric.subValue && (
                  <p className="text-sm text-gray-500 mt-1">
                    {metric.subValue}
                  </p>
                )}
              </div>
              {isLoading && (
                <div className="w-4 h-4 border-2 border-gray-300 border-t-blue-600 rounded-full animate-spin"></div>
              )}
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
} 