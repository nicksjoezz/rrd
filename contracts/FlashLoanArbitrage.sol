// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

/**
 * @title FlashLoanArbitrage
 * @dev Hybrid Sentinel Arbitrage Executor
 * Flow:
 * 1. Flash borrow USDC from Uniswap V3.
 * 2. Buy TokenX on Camelot (V2 or V3/Algebra).
 * 3. Sell TokenX back to USDC on Uniswap V3.
 * 4. Repay flash loan + fee.
 * 5. Keep profit in contract or send to owner.
 */

interface IERC20 {
    function balanceOf(address account) external view returns (uint256);
    function transfer(address to, uint256 amount) external returns (bool);
    function approve(address spender, uint256 amount) external returns (bool);
}

interface IUniswapV3Pool {
    function token0() external view returns (address);
    function token1() external view returns (address);
    function flash(
        address recipient,
        uint256 amount0,
        uint256 amount1,
        bytes calldata data
    ) external;
}

interface ICamelotRouter {
    function swapExactTokensForTokensSupportingFeeOnTransferTokens(
        uint amountIn,
        uint amountOutMin,
        address[] calldata path,
        address to,
        address referrer,
        uint deadline
    ) external;
}

interface ICamelotV3Router {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 limitSqrtPrice;
    }
    function exactInputSingle(ExactInputSingleParams calldata params) external returns (uint256 amountOut);
}

interface ISwapRouter {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }
    function exactInputSingle(ExactInputSingleParams calldata params) external returns (uint256 amountOut);
}

contract FlashLoanArbitrage {
    address public owner;

    // Arbitrum Mainnet Addresses
    address public constant CAMELOT_V2_ROUTER = 0xc873fEcbd354f5A56E00E710B90EF4201db2448d;
    address public constant CAMELOT_V3_ROUTER = 0x1F721E2E82F6676FCE4eA07A5958cF098D339e18; // Algebra
    address public constant UNIV3_ROUTER      = 0xE592427A0AEce92De3Edee1F18E0157C05861564;

    event ArbExecuted(address token, uint256 amountIn, uint256 profit);

    constructor() {
        owner = msg.sender;
    }

    modifier onlyOwner() {
        require(msg.sender == owner, "Not owner");
        _;
    }

    /**
     * @notice Initiates the arbitrage.
     * @param flashPool The UniV3 pool to borrow USDC from.
     * @param tokenX The target token to arbitrage.
     * @param tokenUSDC The USDC token address.
     * @param amountUSDC Amount to borrow.
     * @param uniV3Fee The fee of the UniV3 pool for the sell leg.
     * @param minProfit Minimum profit required (in USDC decimals).
     * @param isCamelotV3 True if using Camelot V3 for the buy leg.
     */
    function executeArb(
        address flashPool,
        address tokenX,
        address tokenUSDC,
        uint256 amountUSDC,
        uint24 uniV3Fee,
        uint256 minProfit,
        bool isCamelotV3
    ) external onlyOwner {
        bool isToken0 = IUniswapV3Pool(flashPool).token0() == tokenUSDC;
        uint256 amount0 = isToken0 ? amountUSDC : 0;
        uint256 amount1 = isToken0 ? 0 : amountUSDC;

        IUniswapV3Pool(flashPool).flash(
            address(this),
            amount0,
            amount1,
            abi.encode(flashPool, tokenX, tokenUSDC, amountUSDC, uniV3Fee, minProfit, isCamelotV3)
        );
    }

    function uniswapV3FlashCallback(
        uint256 fee0,
        uint256 fee1,
        bytes calldata data
    ) external {
        (address flashPool, address tokenX, address tokenUSDC, uint256 amountUSDC, uint24 uniV3Fee, uint256 minProfit, bool isCamelotV3) =
            abi.decode(data, (address, address, address, uint256, uint24, uint256, bool));

        require(msg.sender == flashPool, "Unauthorized callback");

        uint256 fee = fee0 > 0 ? fee0 : fee1;
        uint256 amountToRepay = amountUSDC + fee;
        uint256 balBefore = IERC20(tokenUSDC).balanceOf(address(this));

        // 1. Leg 1: Buy TokenX on Camelot
        if (isCamelotV3) {
            IERC20(tokenUSDC).approve(CAMELOT_V3_ROUTER, amountUSDC);
            ICamelotV3Router(CAMELOT_V3_ROUTER).exactInputSingle(
                ICamelotV3Router.ExactInputSingleParams({
                    tokenIn: tokenUSDC,
                    tokenOut: tokenX,
                    recipient: address(this),
                    deadline: block.timestamp,
                    amountIn: amountUSDC,
                    amountOutMinimum: 0,
                    limitSqrtPrice: 0
                })
            );
        } else {
            IERC20(tokenUSDC).approve(CAMELOT_V2_ROUTER, amountUSDC);
            address[] memory path = new address[](2);
            path[0] = tokenUSDC;
            path[1] = tokenX;

            ICamelotRouter(CAMELOT_V2_ROUTER).swapExactTokensForTokensSupportingFeeOnTransferTokens(
                amountUSDC,
                0,
                path,
                address(this),
                address(0),
                block.timestamp
            );
        }

        // 2. Leg 2: Sell TokenX on UniV3
        uint256 tokenXBal = IERC20(tokenX).balanceOf(address(this));
        IERC20(tokenX).approve(UNIV3_ROUTER, tokenXBal);

        ISwapRouter(UNIV3_ROUTER).exactInputSingle(
            ISwapRouter.ExactInputSingleParams({
                tokenIn: tokenX,
                tokenOut: tokenUSDC,
                fee: uniV3Fee,
                recipient: address(this),
                deadline: block.timestamp,
                amountIn: tokenXBal,
                amountOutMinimum: amountToRepay + minProfit,
                sqrtPriceLimitX96: 0
            })
        );

        // 3. Repay Flash Loan
        IERC20(tokenUSDC).transfer(msg.sender, amountToRepay);

        // 4. Final verification
        uint256 balAfter = IERC20(tokenUSDC).balanceOf(address(this));
        require(balAfter >= balBefore + minProfit, "Insufficient Profit");

        emit ArbExecuted(tokenX, amountUSDC, balAfter - balBefore);

        // Auto-withdraw profit to owner
        if (balAfter > 0) {
            IERC20(tokenUSDC).transfer(owner, balAfter);
        }
    }

    function withdraw(address token) external onlyOwner {
        uint256 bal = IERC20(token).balanceOf(address(this));
        IERC20(token).transfer(owner, bal);
    }

    function withdrawETH() external onlyOwner {
        payable(owner).transfer(address(this).balance);
    }

    receive() external payable {}
}
