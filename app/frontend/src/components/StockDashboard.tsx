import { useState, useEffect } from 'react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from './ui/card';
import { Badge } from './ui/badge';
import { Button } from './ui/button';
import { TrendingUp, TrendingDown, Activity, BarChart3, RefreshCw } from 'lucide-react';
import { API_CONFIG } from '../config/api';

interface StockData {
  symbol: string;
  company_name: string;
  sector: string;
  industry: string;
  current_price: number;
  price_change: number | null;
  price_change_percent: number | null;
  volume: number;
  market_cap: number;
  pe_ratio: number;
  pb_ratio: number;
  revenue_growth: number;
  earnings_growth: number;
  dividend_yield: number;
  day_high: number;
  day_low: number;
  week_52_high: number;
  week_52_low: number;
  chart_data: Array<{
    date: string;
    timestamp: number;
    open: number;
    high: number;
    low: number;
    close: number;
    volume: number;
  }>;
  position?: {
    long_shares: number;
    short_shares: number;
    long_cost_basis: number;
    short_cost_basis: number;
    total_unrealized_gain: number;
    unrealized_gain_percent: number;
    current_long_value: number;
    long_value: number;
  };
}

interface HoldingsData {
  portfolio_id: string;
  holdings: Record<string, StockData>;
  period: string;
  total_holdings: number;
}

export function StockDashboard() {
  const [holdingsData, setHoldingsData] = useState<HoldingsData | null>(null);
  const [selectedPeriod, setSelectedPeriod] = useState('1mo');
  const [isLoading, setIsLoading] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);

  useEffect(() => {
    fetchHoldingsData();
  }, [selectedPeriod]);

  const fetchHoldingsData = async () => {
    setIsLoading(true);
    try {
      const response = await fetch(`${API_CONFIG.BASE_URL}${API_CONFIG.ENDPOINTS.PORTFOLIO_HOLDINGS_DATA}?period=${selectedPeriod}`);
      if (response.ok) {
        const data = await response.json();
        setHoldingsData(data);
        setLastUpdated(new Date());
      } else {
        console.error('Failed to fetch holdings data');
      }
    } catch (error) {
      console.error('Error fetching holdings data:', error);
    } finally {
      setIsLoading(false);
    }
  };

  const formatCurrency = (amount: number) => {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: 'USD',
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(amount);
  };

  const formatLargeNumber = (num: number) => {
    if (num >= 1e12) return `$${(num / 1e12).toFixed(2)}T`;
    if (num >= 1e9) return `$${(num / 1e9).toFixed(2)}B`;
    if (num >= 1e6) return `$${(num / 1e6).toFixed(2)}M`;
    if (num >= 1e3) return `$${(num / 1e3).toFixed(2)}K`;
    return formatCurrency(num);
  };

  const formatPercent = (value: number | null) => {
    if (value === null) return 'N/A';
    return `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;
  };

  const SimpleChart = ({ data }: { data: StockData['chart_data'] }) => {
    if (!data || data.length === 0) return <div>No chart data available</div>;

    const maxPrice = Math.max(...data.map(d => d.high));
    const minPrice = Math.min(...data.map(d => d.low));
    const priceRange = maxPrice - minPrice;
    
    const width = 300;
    const height = 150;
    const padding = 20;

    return (
      <div className="bg-white rounded-lg p-4 border border-gray-200">
        <svg width={width} height={height} className="w-full">
          {/* Grid lines */}
          {[0, 0.25, 0.5, 0.75, 1].map((ratio) => (
            <g key={ratio}>
              <line
                x1={padding}
                y1={padding + ratio * (height - 2 * padding)}
                x2={width - padding}
                y2={padding + ratio * (height - 2 * padding)}
                stroke="#f3f4f6"
                strokeWidth="1"
              />
            </g>
          ))}
          
          {/* Price line */}
          <polyline
            points={data.map((point, index) => {
              const x = padding + (index / (data.length - 1)) * (width - 2 * padding);
              const y = padding + (1 - (point.close - minPrice) / priceRange) * (height - 2 * padding);
              return `${x},${y}`;
            }).join(' ')}
            fill="none"
            stroke="#2563eb"
            strokeWidth="3"
          />
          
          {/* Data points */}
          {data.map((point, index) => {
            const x = padding + (index / (data.length - 1)) * (width - 2 * padding);
            const y = padding + (1 - (point.close - minPrice) / priceRange) * (height - 2 * padding);
            return (
              <circle
                key={index}
                cx={x}
                cy={y}
                r="4"
                fill="#2563eb"
                className="hover:r-5 transition-all"
              />
            );
          })}
        </svg>
        
        <div className="mt-3 flex justify-between text-sm text-gray-700 font-medium">
          <span>{data[0]?.date}</span>
          <span>{data[data.length - 1]?.date}</span>
        </div>
      </div>
    );
  };

  if (!holdingsData) {
    return (
      <div className="p-6 space-y-6 bg-white min-h-screen">
        <div className="flex justify-between items-center">
          <div>
            <h1 className="text-3xl font-bold mb-2 text-gray-900">Stock Dashboard</h1>
            <p className="text-gray-600">Real-time market data and portfolio analytics</p>
          </div>
          <Button onClick={fetchHoldingsData} disabled={isLoading}>
            {isLoading ? <RefreshCw className="w-4 h-4 animate-spin mr-2" /> : <RefreshCw className="w-4 h-4 mr-2" />}
            {isLoading ? 'Loading...' : 'Load Data'}
          </Button>
        </div>
        
        {!isLoading && (
          <Card>
            <CardContent className="flex items-center justify-center py-12">
              <div className="text-center">
                <Activity className="w-12 h-12 text-gray-400 mx-auto mb-4" />
                <p className="text-gray-600">No holdings found. Add some positions to your portfolio first.</p>
              </div>
            </CardContent>
          </Card>
        )}
      </div>
    );
  }

  return (
    <div className="p-6 space-y-6 bg-white min-h-screen">
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-3xl font-bold mb-2 text-gray-900">Stock Dashboard</h1>
          <p className="text-gray-600">Real-time market data and portfolio analytics</p>
          {lastUpdated && (
            <p className="text-sm text-gray-500 mt-1">
              Last updated: {lastUpdated.toLocaleTimeString()}
            </p>
          )}
        </div>
        
        <div className="flex items-center gap-4">
          <select
            value={selectedPeriod}
            onChange={(e) => setSelectedPeriod(e.target.value)}
            className="px-3 py-2 border border-gray-300 rounded-md bg-white text-gray-900"
          >
            <option value="1d">1 Day</option>
            <option value="5d">5 Days</option>
            <option value="1mo">1 Month</option>
            <option value="3mo">3 Months</option>
            <option value="6mo">6 Months</option>
            <option value="1y">1 Year</option>
          </select>
          
          <Button onClick={fetchHoldingsData} disabled={isLoading} variant="outline">
            {isLoading ? <RefreshCw className="w-4 h-4 animate-spin mr-2" /> : <RefreshCw className="w-4 h-4 mr-2" />}
            {isLoading ? 'Updating...' : 'Refresh'}
          </Button>
        </div>
      </div>

      {/* Portfolio Summary */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <BarChart3 className="w-5 h-5" />
            Portfolio Summary
          </CardTitle>
          <CardDescription>Overview of your holdings ({holdingsData.total_holdings} stocks)</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
            {Object.values(holdingsData.holdings).map((stock) => {
              if (!stock.position) return null;
              
              const isGaining = stock.position.total_unrealized_gain >= 0;
              
              return (
                <div key={stock.symbol} className="bg-white rounded-lg p-4 border border-gray-200 shadow-sm">
                  <div className="flex justify-between items-start mb-2">
                    <span className="font-bold text-gray-900">{stock.symbol}</span>
                    <Badge variant="outline" className="text-xs bg-blue-50 text-blue-700 border-blue-200">
                      {stock.position.long_shares} shares
                    </Badge>
                  </div>
                  <div className="space-y-2">
                    <div className="text-xl font-bold text-gray-900">
                      {formatCurrency(stock.current_price)}
                    </div>
                    <div className={`text-sm font-semibold ${isGaining ? 'text-green-700' : 'text-red-600'}`}>
                      {isGaining ? <TrendingUp className="w-3 h-3 inline mr-1" /> : <TrendingDown className="w-3 h-3 inline mr-1" />}
                      {formatCurrency(stock.position.total_unrealized_gain)}
                      <span className="ml-1">
                        ({formatPercent(stock.position.unrealized_gain_percent)})
                      </span>
                    </div>
                    <div className="text-xs text-gray-600 font-medium">
                      Cost: {formatCurrency(stock.position.long_cost_basis)}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </CardContent>
      </Card>

      {/* Individual Stock Details */}
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        {Object.values(holdingsData.holdings).map((stock) => (
          <Card key={stock.symbol} className="bg-white border-gray-200">
            <CardHeader>
              <div className="flex justify-between items-start">
                <div>
                  <CardTitle className="flex items-center gap-3">
                    <span className="text-2xl font-bold text-gray-900">{stock.symbol}</span>
                    <Badge variant="secondary" className="text-xs bg-gray-100 text-gray-700">
                      {stock.sector}
                    </Badge>
                  </CardTitle>
                  <CardDescription className="text-gray-600 font-medium">
                    {stock.company_name} • {stock.industry}
                  </CardDescription>
                </div>
                <div className="text-right">
                  <div className="text-3xl font-bold text-gray-900">
                    {formatCurrency(stock.current_price)}
                  </div>
                  {stock.position && (
                    <div className={`text-base font-bold ${stock.position.total_unrealized_gain >= 0 ? 'text-green-700' : 'text-red-600'}`}>
                      {stock.position.total_unrealized_gain >= 0 ? '+' : ''}{formatCurrency(stock.position.total_unrealized_gain)}
                    </div>
                  )}
                </div>
              </div>
            </CardHeader>
            
            <CardContent className="space-y-6">
              {/* Price Chart */}
              <div>
                <h4 className="font-semibold mb-3 text-gray-900">Price Chart ({selectedPeriod})</h4>
                <SimpleChart data={stock.chart_data} />
              </div>
              
              {/* Position Details */}
              {stock.position && (
                <div>
                  <h4 className="font-semibold mb-3 text-gray-900">Position Details</h4>
                  <div className="grid grid-cols-2 gap-4 text-sm">
                    <div className="flex justify-between items-center">
                      <span className="text-gray-600">Shares:</span>
                      <span className="font-semibold text-gray-900">{stock.position.long_shares}</span>
                    </div>
                    <div className="flex justify-between items-center">
                      <span className="text-gray-600">Cost Basis:</span>
                      <span className="font-semibold text-gray-900">{formatCurrency(stock.position.long_cost_basis)}</span>
                    </div>
                    <div className="flex justify-between items-center">
                      <span className="text-gray-600">Original Value:</span>
                      <span className="font-semibold text-gray-900">{formatCurrency(stock.position.long_value)}</span>
                    </div>
                    <div className="flex justify-between items-center">
                      <span className="text-gray-600">Current Value:</span>
                      <span className="font-semibold text-green-700">{formatCurrency(stock.position.current_long_value)}</span>
                    </div>
                  </div>
                </div>
              )}
              
              {/* Market Data */}
              <div>
                <h4 className="font-semibold mb-3 text-gray-900">Market Data</h4>
                <div className="grid grid-cols-2 gap-4 text-sm">
                  <div className="flex justify-between items-center">
                    <span className="text-gray-600">Volume:</span>
                    <span className="font-semibold text-gray-900">{(stock.volume || 0).toLocaleString()}</span>
                  </div>
                  <div className="flex justify-between items-center">
                    <span className="text-gray-600">Market Cap:</span>
                    <span className="font-semibold text-blue-700">{formatLargeNumber(stock.market_cap || 0)}</span>
                  </div>
                  <div className="flex justify-between items-center">
                    <span className="text-gray-600">Day Range:</span>
                    <span className="font-semibold text-gray-900">
                      {formatCurrency(stock.day_low)} - {formatCurrency(stock.day_high)}
                    </span>
                  </div>
                  <div className="flex justify-between items-center">
                    <span className="text-gray-600">52W Range:</span>
                    <span className="font-semibold text-gray-900">
                      {formatCurrency(stock.week_52_low)} - {formatCurrency(stock.week_52_high)}
                    </span>
                  </div>
                </div>
              </div>
              
              {/* Financial Ratios */}
              <div>
                <h4 className="font-semibold mb-3 text-gray-900">Key Ratios</h4>
                <div className="grid grid-cols-2 gap-4 text-sm">
                  <div className="flex justify-between items-center">
                    <span className="text-gray-600">P/E Ratio:</span>
                    <span className="font-semibold text-gray-900">{stock.pe_ratio?.toFixed(2) || 'N/A'}</span>
                  </div>
                  <div className="flex justify-between items-center">
                    <span className="text-gray-600">P/B Ratio:</span>
                    <span className="font-semibold text-gray-900">{stock.pb_ratio?.toFixed(2) || 'N/A'}</span>
                  </div>
                  <div className="flex justify-between items-center">
                    <span className="text-gray-600">Revenue Growth:</span>
                    <span className={`font-semibold ${stock.revenue_growth && stock.revenue_growth > 0 ? 'text-green-700' : stock.revenue_growth && stock.revenue_growth < 0 ? 'text-red-600' : 'text-gray-900'}`}>
                      {formatPercent(stock.revenue_growth ? stock.revenue_growth * 100 : null)}
                    </span>
                  </div>
                  <div className="flex justify-between items-center">
                    <span className="text-gray-600">Dividend Yield:</span>
                    <span className="font-semibold text-purple-700">
                      {formatPercent(stock.dividend_yield ? stock.dividend_yield * 100 : null)}
                    </span>
                  </div>
                </div>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
} 