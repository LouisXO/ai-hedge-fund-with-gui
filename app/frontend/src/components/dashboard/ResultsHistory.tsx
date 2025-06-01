import { Button } from '../ui/button';

interface TradingDecision {
  agent: string;
  recommendation: 'BUY' | 'SELL' | 'HOLD';
  confidence: number;
  reasoning?: string;
  timestamp: string;
}

interface TradingResult {
  id: string;
  tickers: string[];
  decisions: TradingDecision[];
  portfolio: any;
  timestamp: string;
  performance?: {
    totalReturn: number;
    sharpeRatio: number;
    maxDrawdown: number;
  };
}

interface ResultsHistoryProps {
  results: TradingResult[];
  onSelectResult: (result: TradingResult) => void;
}

export function ResultsHistory({ results, onSelectResult }: ResultsHistoryProps) {
  if (results.length === 0) {
    return (
      <div className="text-center py-8 text-gray-500">
        No analysis history available. Run your first analysis to see results here.
      </div>
    );
  }

  const getOverallRecommendation = (decisions: TradingDecision[]) => {
    if (!decisions || decisions.length === 0) return 'N/A';
    
    const counts = decisions.reduce((acc, decision) => {
      acc[decision.recommendation] = (acc[decision.recommendation] || 0) + 1;
      return acc;
    }, {} as Record<string, number>);

    return Object.entries(counts).reduce((a, b) => counts[a[0]] > counts[b[0]] ? a : b)[0];
  };

  const getAverageConfidence = (decisions: TradingDecision[]) => {
    if (!decisions || decisions.length === 0) return 0;
    const total = decisions.reduce((sum, decision) => sum + decision.confidence, 0);
    return total / decisions.length;
  };

  const formatRelativeTime = (timestamp: string) => {
    const now = new Date();
    const past = new Date(timestamp);
    const diffMs = now.getTime() - past.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMs / 3600000);
    const diffDays = Math.floor(diffMs / 86400000);

    if (diffMins < 1) return 'Just now';
    if (diffMins < 60) return `${diffMins}m ago`;
    if (diffHours < 24) return `${diffHours}h ago`;
    return `${diffDays}d ago`;
  };

  return (
    <div className="space-y-3">
      {results.map((result) => {
        const overallRec = getOverallRecommendation(result.decisions);
        const avgConfidence = getAverageConfidence(result.decisions);
        
        return (
          <div 
            key={result.id}
            className="border rounded-lg p-4 hover:bg-gray-50 transition-colors cursor-pointer"
            onClick={() => onSelectResult(result)}
          >
            <div className="flex items-center justify-between mb-2">
              <div className="flex items-center gap-3">
                <span className="font-medium">
                  {result.tickers.join(', ')}
                </span>
                <span className={`px-2 py-1 rounded text-xs font-medium ${
                  overallRec === 'BUY' ? 'bg-green-100 text-green-800' :
                  overallRec === 'SELL' ? 'bg-red-100 text-red-800' :
                  'bg-yellow-100 text-yellow-800'
                }`}>
                  {overallRec}
                </span>
              </div>
              <div className="text-sm text-gray-500">
                {formatRelativeTime(result.timestamp)}
              </div>
            </div>
            
            <div className="flex items-center justify-between text-sm text-gray-600">
              <span>
                {result.decisions.length} agent{result.decisions.length !== 1 ? 's' : ''}
              </span>
              <span>
                Avg confidence: {Math.round(avgConfidence * 100)}%
              </span>
            </div>

            {result.performance && (
              <div className="mt-2 grid grid-cols-3 gap-2 text-xs">
                <div className="text-center p-2 bg-blue-50 rounded">
                  <div className="font-medium">Return</div>
                  <div className={result.performance.totalReturn >= 0 ? 'text-green-600' : 'text-red-600'}>
                    {result.performance.totalReturn >= 0 ? '+' : ''}{result.performance.totalReturn.toFixed(2)}%
                  </div>
                </div>
                <div className="text-center p-2 bg-blue-50 rounded">
                  <div className="font-medium">Sharpe</div>
                  <div>{result.performance.sharpeRatio.toFixed(2)}</div>
                </div>
                <div className="text-center p-2 bg-blue-50 rounded">
                  <div className="font-medium">Max DD</div>
                  <div className="text-red-600">
                    -{result.performance.maxDrawdown.toFixed(2)}%
                  </div>
                </div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
} 