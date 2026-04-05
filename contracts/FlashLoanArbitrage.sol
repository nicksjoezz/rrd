// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

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
    address public constant CAMELOT_ROUTER = 0xc873fEcbd354f5A56E00E710B90EF4201db2448d;
    address public constant UNIV3_ROUTER    = 0xE592427A0AEce92De3Edee1F18E0157C05861564;

    constructor() {
        owner = msg.sender;
    }

    modifier onlyOwner() {
        require(msg.sender == owner, "Not owner");
        _;
    }

    function executeArb(
        address flashPool,
        address tokenX,
        address tokenUSDC,
        uint256 amountUSDC,
        uint24 uniV3Fee,
        uint256 minProfit
    ) external onlyOwner {
        bool isToken0 = IUniswapV3Pool(flashPool).token0() == tokenUSDC;
        uint256 amount0 = isToken0 ? amountUSDC : 0;
        uint256 amount1 = isToken0 ? 0 : amountUSDC;

        IUniswapV3Pool(flashPool).flash(
            address(this),
            amount0,
            amount1,
            abi.encode(flashPool, tokenX, tokenUSDC, amountUSDC, uniV3Fee, minProfit)
        );
    }

    function uniswapV3FlashCallback(
        uint256 fee0,
        uint256 fee1,
        bytes calldata data
    ) external {
        (address flashPool, address tokenX, address tokenUSDC, uint256 amountUSDC, uint24 uniV3Fee, uint256 minProfit) =
            abi.decode(data, (address, address, address, uint256, uint24, uint256));

        require(msg.sender == flashPool, "Unauthorized callback");

        uint256 fee = fee0 > 0 ? fee0 : fee1;
        uint256 amountToRepay = amountUSDC + fee;

        // 1. Buy TokenX on Camelot
        IERC20(tokenUSDC).approve(CAMELOT_ROUTER, amountUSDC);
        address[] memory path = new address[](2);
        path[0] = tokenUSDC;
        path[1] = tokenX;

        ICamelotRouter(CAMELOT_ROUTER).swapExactTokensForTokensSupportingFeeOnTransferTokens(
            amountUSDC,
            0, // minAmountOut (slippage handled by minProfit check at the end)
            path,
            address(this),
            address(0),
            block.timestamp
        );

        // 2. Sell TokenX on UniV3
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

        // 4. Send profit to owner
        uint256 profit = IERC20(tokenUSDC).balanceOf(address(this));
        if (profit > 0) {
            IERC20(tokenUSDC).transfer(owner, profit);
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
