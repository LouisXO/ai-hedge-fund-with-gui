interface TradingDecision {
  agent: string;
  recommendation: 'BUY' | 'SELL' | 'HOLD';
  confidence: number;
  reasoning?: string;
  timestamp: string;
}

interface AgentDecisionsPanelProps {
  decisions: TradingDecision[];
  tickers: string[];
}

export function AgentDecisionsPanel({ decisions, tickers }: AgentDecisionsPanelProps) {
  if (!decisions || decisions.length === 0) {
    return (
      <div className="text-center py-8 text-gray-500">
        No trading decisions available
      </div>
    );
  }

  const getRecommendationColor = (recommendation: string) => {
    switch (recommendation) {
      case 'BUY':
        return 'bg-green-100 text-green-800 border-green-200';
      case 'SELL':
        return 'bg-red-100 text-red-800 border-red-200';
      case 'HOLD':
        return 'bg-yellow-100 text-yellow-800 border-yellow-200';
      default:
        return 'bg-gray-100 text-gray-800 border-gray-200';
    }
  };

  const getConfidenceColor = (confidence: number) => {
    if (confidence >= 0.8) return 'text-green-600';
    if (confidence >= 0.6) return 'text-yellow-600';
    return 'text-red-600';
  };

  // Group decisions by ticker
  const decisionsByTicker = decisions.reduce((acc, decision) => {
    const ticker = decision.ticker || 'OVERALL';
    if (!acc[ticker]) acc[ticker] = [];
    acc[ticker].push(decision);
    return acc;
  }, {} as Record<string, TradingDecision[]>);

  return (
    <div className="space-y-6">
      {Object.entries(decisionsByTicker).map(([ticker, tickerDecisions]) => (
        <div key={ticker} className="border rounded-lg p-4">
          <h3 className="text-lg font-semibold mb-4 text-blue-600">
            {ticker === 'OVERALL' ? 'Overall Portfolio' : ticker}
          </h3>
          
          <div className="grid gap-4">
            {tickerDecisions.map((decision, index) => (
              <div key={index} className="border rounded-lg p-4 bg-white shadow-sm">
                <div className="flex items-center justify-between mb-3">
                  <div className="flex items-center gap-3">
                    <span className="font-medium text-gray-900">
                      {decision.agent.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase())}
                    </span>
                    <span 
                      className={`px-2 py-1 rounded-full text-sm font-medium border ${getRecommendationColor(decision.recommendation)}`}
                    >
                      {decision.recommendation}
                    </span>
                  </div>
                  <div className="text-right">
                    <div className={`text-lg font-bold ${getConfidenceColor(decision.confidence)}`}>
                      {Math.round(decision.confidence * 100)}%
                    </div>
                    <div className="text-xs text-gray-500">confidence</div>
                  </div>
                </div>
                
                {decision.reasoning && (
                  <div className="mt-3">
                    <h4 className="text-sm font-medium text-gray-700 mb-2">Reasoning:</h4>
                    <p className="text-sm text-gray-600 leading-relaxed bg-gray-50 p-3 rounded">
                      {decision.reasoning}
                    </p>
                  </div>
                )}
                
                {decision.timestamp && (
                  <div className="mt-3 text-xs text-gray-400">
                    {new Date(decision.timestamp).toLocaleString()}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      ))}

      {/* Summary Section */}
      <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
        <h3 className="text-lg font-semibold mb-3 text-blue-800">Decision Summary</h3>
        <div className="grid grid-cols-3 gap-4 text-center">
          {['BUY', 'SELL', 'HOLD'].map(rec => {
            const count = decisions.filter(d => d.recommendation === rec).length;
            const percentage = decisions.length > 0 ? (count / decisions.length * 100).toFixed(1) : '0';
            
            return (
              <div key={rec} className="bg-white rounded p-3">
                <div className={`text-2xl font-bold ${getRecommendationColor(rec).split(' ')[1]}`}>
                  {count}
                </div>
                <div className="text-sm text-gray-600">{rec} ({percentage}%)</div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
} 