export function PerformanceChart() {
  // Mock performance data
  const performanceData = [
    { period: '1D', value: '+0.85%', color: 'text-green-600' },
    { period: '1W', value: '+2.31%', color: 'text-green-600' },
    { period: '1M', value: '+6.38%', color: 'text-green-600' },
    { period: '3M', value: '+12.45%', color: 'text-green-600' },
    { period: '1Y', value: '+24.76%', color: 'text-green-600' },
  ];

  return (
    <div className="space-y-4">
      {/* Performance Periods */}
      <div className="space-y-2">
        {performanceData.map((item) => (
          <div key={item.period} className="flex justify-between items-center py-2">
            <span className="text-sm text-gray-600">{item.period}</span>
            <span className={`font-semibold ${item.color}`}>{item.value}</span>
          </div>
        ))}
      </div>

      {/* Simple visual chart placeholder */}
      <div className="bg-gradient-to-r from-green-100 to-green-200 h-32 rounded-lg flex items-center justify-center">
        <div className="text-center">
          <div className="text-2xl font-bold text-green-700">📈</div>
          <div className="text-sm text-green-600 mt-1">Performance Chart</div>
          <div className="text-xs text-green-500 mt-1">Charts coming soon</div>
        </div>
      </div>
    </div>
  );
} 