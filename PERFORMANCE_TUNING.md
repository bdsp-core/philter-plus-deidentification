# Performance Tuning Guide

## Hardware Specs
- **RAM:** 192GB
- **CPU:** 2x Intel Xeon Silver 4216 @ 2.10GHz
- **Total Cores:** 32 cores / 64 threads

## Optimizations Implemented

### 1. Parallel Processing
- Uses Python multiprocessing.Pool to leverage all CPU cores
- Each worker initializes Philter **once** and reuses it for all batches
- Previous version: 1.1 records/sec (sequential)
- Optimized version: **Expected 30-50+ records/sec** (parallel)

### 2. Batch Processing
- Groups records into batches before processing
- Reduces Philter initialization overhead
- More efficient coordinate mapping

### 3. Database Optimization
- Keyset pagination for efficient fetching
- Bulk inserts with periodic commits
- Checks for already-processed records to enable resume

## Configuration Parameters

Edit `db_deidentify_optimized.py` to tune these parameters:

### `num_workers` (Line 132)
**Current:** 40 workers
**Recommended range:** 32-48
- More workers = faster processing but higher memory usage
- Leave 8-16 cores for database, OS, and other processes
- Each worker uses ~500MB-1GB RAM
- With 192GB RAM, you can easily run 40-60 workers

**How to adjust:**
```python
num_workers = 40  # Start here
# If CPU usage < 80%, increase to 48
# If memory usage > 90%, decrease to 32
```

### `batch_size` (Line 134)
**Current:** 100 records per batch
**Recommended range:** 50-200
- Larger batches = better Philter efficiency
- Too large = slower individual batch processing
- 100 is a good sweet spot for most workloads

**How to adjust:**
```python
batch_size = 100  # Start here
# For shorter notes (< 1000 chars), try 150-200
# For longer notes (> 5000 chars), try 50-75
```

### `fetch_batch_size` (Line 136)
**Current:** 5000 records
**Recommended range:** 2000-10000
- How many records to fetch from DB at once
- Larger = fewer database round-trips
- Too large = more memory usage

**How to adjust:**
```python
fetch_batch_size = 5000  # Good default
# If plenty of RAM, increase to 10000
# If records are very large, decrease to 2000
```

### `commit_every` (Line 138)
**Current:** 500 records
**Recommended range:** 250-1000
- How often to commit to database
- More frequent = safer but slower
- Less frequent = faster but risk losing more on crash

## Expected Performance

### Conservative Estimate
- 40 workers × 1.1 records/sec = **44 records/sec**
- 1 million records = **6.3 hours**

### Realistic Estimate (with optimizations)
- With batch processing efficiency gains
- **50-70 records/sec**
- 1 million records = **4-5.5 hours**

### Best Case
- If notes are short and patterns match well
- **80-100 records/sec**
- 1 million records = **2.8-3.5 hours**

## Monitoring Performance

While the script runs, watch for:

1. **CPU Usage**
   - Target: 70-90% total CPU usage
   - If < 70%: Increase `num_workers`
   - If > 95%: Decrease `num_workers` slightly

2. **Memory Usage**
   - Target: 50-80% of 192GB
   - If approaching 90%: Decrease `num_workers` or `batch_size`

3. **Records/sec**
   - The script logs average speed
   - Should stabilize after first few batches
   - Fluctuations are normal

4. **Database Wait**
   - If "Fetched ... records" takes > 5 seconds
   - Consider increasing `fetch_batch_size`

## Running the Optimized Script

```bash
# Full production run
python db_deidentify_optimized.py

# Test with a small batch first
# (modify the script to add LIMIT in the query)
```

## Troubleshooting

### "Worker failed to initialize"
- Check that configs/philter_one.json exists
- Verify regex patterns compile correctly

### "Memory error" or system slowdown
- Reduce `num_workers` to 24-32
- Reduce `batch_size` to 50-75
- Reduce `fetch_batch_size` to 2000

### Slow performance (< 20 records/sec)
- Increase `num_workers` to 48-50
- Check CPU usage - should be > 70%
- Check database server load

### Database connection issues
- Reduce `num_workers` to 20-30
- Database server may be the bottleneck
- Check network latency to 172.18.160.211

## Comparison: Old vs New

| Metric | Old (Sequential) | New (Optimized) |
|--------|-----------------|-----------------|
| Workers | 1 | 40 |
| Philter Init | Every record | Once per worker |
| Batch Processing | No | Yes (100 records) |
| Speed | 1.1 rec/sec | 50+ rec/sec |
| 1M records | ~10 days | ~5 hours |

## Next Steps

1. Run a test with the optimized script on 5000-10000 records
2. Monitor CPU, memory, and speed
3. Adjust `num_workers` and `batch_size` based on observations
4. Run full production workload
