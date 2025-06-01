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

interface AlertsPanelProps {
  holdings: PortfolioHolding[];
}

interface Alert {
  id: string;
  type: 'warning' | 'info' | 'success' | 'error';
  title: string;
  message: string;
  timestamp: string;
  ticker?: string;
}

export function AlertsPanel({ holdings }: AlertsPanelProps) {
  // Generate alerts based on portfolio data
  const generateAlerts = (): Alert[] => {
    const alerts: Alert[] = [];
    const now = new Date().toISOString();

    // Check for significant losses
    holdings.forEach(holding => {
      if (holding.unrealizedPnLPercent < -5) {
        alerts.push({
          id: `loss-${holding.ticker}`,
          type: 'warning',
          title: 'Significant Loss',
          message: `${holding.ticker} is down ${Math.abs(holding.unrealizedPnLPercent).toFixed(1)}%`,
          timestamp: now,
          ticker: holding.ticker,
        });
      }

      // Check for concentration risk
      if (holding.weight > 30) {
        alerts.push({
          id: `concentration-${holding.ticker}`,
          type: 'info',
          title: 'Concentration Risk',
          message: `${holding.ticker} represents ${holding.weight.toFixed(1)}% of your portfolio`,
          timestamp: now,
          ticker: holding.ticker,
        });
      }
    });

    // Add some sample alerts
    alerts.push({
      id: 'market-update',
      type: 'info',
      title: 'Market Update',
      message: 'AI analysts suggest reviewing positions in tech sector',
      timestamp: now,
    });

    return alerts.slice(0, 5); // Limit to 5 alerts
  };

  const alerts = generateAlerts();

  const getAlertIcon = (type: string) => {
    switch (type) {
      case 'warning':
        return '⚠️';
      case 'error':
        return '🚨';
      case 'success':
        return '✅';
      case 'info':
      default:
        return 'ℹ️';
    }
  };

  const getAlertStyles = (type: string) => {
    switch (type) {
      case 'warning':
        return 'border-yellow-200 bg-yellow-50';
      case 'error':
        return 'border-red-200 bg-red-50';
      case 'success':
        return 'border-green-200 bg-green-50';
      case 'info':
      default:
        return 'border-blue-200 bg-blue-50';
    }
  };

  if (alerts.length === 0) {
    return (
      <div className="text-center py-8 text-gray-500">
        <div className="text-2xl mb-2">😌</div>
        <div>No alerts at this time</div>
        <div className="text-sm mt-1">Your portfolio looks good!</div>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {alerts.map((alert) => (
        <div 
          key={alert.id}
          className={`border rounded-lg p-3 ${getAlertStyles(alert.type)}`}
        >
          <div className="flex items-start gap-3">
            <span className="text-lg">{getAlertIcon(alert.type)}</span>
            <div className="flex-1 min-w-0">
              <div className="flex items-center justify-between mb-1">
                <h4 className="font-medium text-gray-900 text-sm">
                  {alert.title}
                </h4>
                {alert.ticker && (
                  <span className="text-xs font-medium text-blue-600 bg-blue-100 px-2 py-1 rounded">
                    {alert.ticker}
                  </span>
                )}
              </div>
              <p className="text-sm text-gray-600 leading-relaxed">
                {alert.message}
              </p>
              <p className="text-xs text-gray-400 mt-2">
                {new Date(alert.timestamp).toLocaleTimeString()}
              </p>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
} 